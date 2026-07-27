/* ═══════════════════════════════════════════
   paper/video.js — Paper Video Abstract tab

   Turns a paper report into a short narrated MG video
   (docs/MOTION_VIDEO_DESIGN.md, P3). Server owns the task
   (POST /api/v1/paper/video/start → motion engine); this module
   renders + polls, and hosts the per-scene preview/regen panel
   (the backend rides /api/v1/motion/videos/* directly).

   States rendered into #paperVideoContent:
     idle / generating (phase progress) / done (player + scene grid)
     report_required (chain to the Report tab) / lookup_failed / error
   ═══════════════════════════════════════════ */

// ── State ──
var _pvideo = {
  paperHash: '',
  lang: 'zh',
  voice: '',
  narration: true,
  burnIn: false,
  quality: 'standard',
  taskId: '',
  cursor: 0,
  pollTimer: null,
  status: 'idle',          // idle|loading|generating|done|report_required|lookup_failed|lost|interrupted|error
  errorText: '',
  progress: { done: 0, total: 0, phase: '' },
  result: null,            // poll done → {final_path, duration, scenes, narrated}
  scenes: [],              // GET /scenes payload
  regenSceneId: '',
  regenTaskId: '',
  ttsAvailable: true,
  defaultVoice: '',
  // P-UX progress perception (docs/PAPER_MEDIA_UX_DESIGN.md §3.4)
  pollFails: 0,
  phases: [],
  phaseIndex: 0,
  genStartedAt: 0,
  lastEventAt: 0,
  tickTimer: null,
  _rateFirstTick: 0,       // wall-clock of the first countable event (ETA)
  _rateFirstDone: 0,
  etaSec: 0,
  _gridLoaded: false,      // scenes skeleton already fetched this run
};
// Poll cadence — a var (not const) so the JSDOM harness can shrink it.
var _PVIDEO_POLL_MS = 1500;
var _PV_POLL_FAIL_LIMIT = 5;

function _pvResetRun() {
  _pvideo.pollFails = 0;
  _pvideo.phases = [];
  _pvideo.phaseIndex = 0;
  _pvideo.genStartedAt = Date.now();
  _pvideo.lastEventAt = Date.now();
  _pvideo._rateFirstTick = 0;
  _pvideo._rateFirstDone = 0;
  _pvideo.etaSec = 0;
  _pvideo._gridLoaded = false;
  _pvideo.scenes = [];
}

function _pvT(key, fallback) {
  return (typeof t === 'function') ? t(key) : (fallback || key);
}

function _pvEl() { return document.getElementById('paperVideoContent'); }

function _pvEsc(s) {
  return (typeof escapeHtml === 'function') ? escapeHtml(s == null ? '' : s)
    : String(s == null ? '' : s);
}

/* Stop the poll timer ONLY — see the note in podcast.js:_pcStopPoll: the 1s
 * activity ticker must survive _pvSchedulePoll()'s re-arm, or the elapsed /
 * last-activity stopwatch freezes at 0:00 on the first poll. */
function _pvStopPoll() {
  if (_pvideo.pollTimer) { clearTimeout(_pvideo.pollTimer); _pvideo.pollTimer = null; }
}

/** Terminal teardown: stop polling AND the ticker (run is over). */
function _pvStopPolling() {
  _pvStopPoll();
  _pvStopTick();
}

function _pvStartTick() {
  if (_pvideo.tickTimer) return;
  _pvideo.tickTimer = setInterval(_pvRenderActivity, 1000);
}

function _pvStopTick() {
  if (_pvideo.tickTimer) { clearInterval(_pvideo.tickTimer); _pvideo.tickTimer = null; }
}

/** Entry point — called by _switchPaperTab('video'). */
async function _initVideoTab(force) {
  var host = _pvEl();
  if (!host) return;
  _pvStopPolling();
  _pvideo.paperHash = (typeof _paperHash !== 'undefined') ? (_paperHash || '') : '';
  var initHash = _pvideo.paperHash;
  if (!_pvideo.paperHash) {
    _pvideo.status = 'idle';
    host.innerHTML = '<div class="paper-report-empty"><p>' +
      _pvEsc(_pvT('paper.reportNoText', 'No paper text available. Load a PDF first.')) +
      '</p></div>';
    return;
  }
  _pvideo.status = 'loading';
  _pvRender();
  try {
    var st = await Api.motion.status();
    /* Paper switched mid-lookup — drop the stale continuation (pt_3cd6cd48). */
    if (_pvideo.paperHash !== initHash) return;
    if (st && st.ok) {
      _pvideo.ttsAvailable = !!st.tts_available;
    }
    var look = await Api.paper.videoLookup({ paper_hash: _pvideo.paperHash });
    if (_pvideo.paperHash !== initHash) return;
    if (look && look.ok && look.found) {
      _pvideo._doneTaskId = look.task_id;
      if (look.running) {
        _pvideo.taskId = look.task_id;
        _pvideo.cursor = 0;
        _pvideo.status = 'generating';
        _pvResetRun();
        _pvRender();
        _pvSchedulePoll();
        return;
      }
      // P-UX4: previous run cut by a server restart — honest state.
      if (look.interrupted) {
        _pvideo.status = 'interrupted';
        _pvRender();
        return;
      }
      if (look.result) {
        _pvideo.result = look.result;
        _pvideo.status = 'done';
        _pvRender();
        _pvLoadScenes();
        return;
      }
    }
    if (look && look.ok) {
      _pvideo.status = look.report_available ? 'idle' : 'report_required';
    } else {
      _pvideo.status = 'lookup_failed';
      _pvideo.errorText = _pvT('paper.videoLookupFailed',
        'Video status lookup failed — check the server log.');
    }
    _pvRender();
  } catch (e) {
    console.warn('[Paper:Video] lookup failed:', e);
    _pvideo.status = 'lookup_failed';
    _pvideo.errorText = String(e && e.message || e);
    _pvRender();
  }
}

function _pvSchedulePoll() {
  _pvStopPoll();
  _pvideo.pollTimer = setTimeout(_pvPollOnce, _PVIDEO_POLL_MS);
}

/** Wall-clock-rate ETA (拍板 A — render/TTS phases only, never invented). */
function _pvEtaTick(done, total) {
  var now = Date.now();
  if (!_pvideo._rateFirstTick) {
    _pvideo._rateFirstTick = now;
    _pvideo._rateFirstDone = done;
  }
  var elapsed = (now - _pvideo._rateFirstTick) / 1000;
  var made = done - _pvideo._rateFirstDone;
  if (made > 0 && total > done) {
    _pvideo.etaSec = Math.round(elapsed / made * (total - done));
  } else {
    _pvideo.etaSec = 0;
  }
}

function _pvConsumeEvent(ev) {
  if (ev.type === 'phase_started') {
    _pvideo.phases = ev.phases || _pvideo.phases;
    _pvideo.phaseIndex = ev.phase_index || 0;
    _pvideo.progress.phase = ev.phase || _pvideo.progress.phase;
    _pvideo._rateFirstTick = 0;
    _pvideo._rateFirstDone = 0;
    _pvideo.etaSec = 0;
    // The storyboard is on disk from here on — pull the grid skeleton.
    if ((ev.phase === 'compose' || ev.phase === 'render') && !_pvideo._gridLoaded) {
      _pvideo._gridLoaded = true;
      _pvLoadScenes();
    }
    return true;   // phase changed → the stepper needs a full re-render
  } else if (ev.type === 'phase') {
    _pvideo.progress.phase = ev.phase || _pvideo.progress.phase;
  } else if (ev.type === 'progress') {
    _pvideo.progress = { done: ev.done || 0, total: ev.total || 0,
                         phase: ev.phase || _pvideo.progress.phase };
    if (ev.phase === 'narrate') _pvEtaTick(ev.done || 0, ev.total || 0);
  } else if (ev.type === 'scene_done') {
    _pvideo.progress = { done: ev.done || 0, total: ev.total || 0,
                         phase: 'render' };
    _pvEtaTick(ev.done || 0, ev.total || 0);
    // P-UX3: light the scene up in the grid the moment it lands.
    _pvLoadScenes();
  }
  // 'heartbeat' needs no handling — any event already bumps lastEventAt.
}

/** P-UX1 terminal backstop: 5 consecutive poll failures → lost state. */
function _pvPollFail() {
  _pvideo.pollFails++;
  if (_pvideo.pollFails >= _PV_POLL_FAIL_LIMIT) {
    _pvStopPolling();
    _pvideo.taskId = '';
    _pvideo.regenTaskId = '';
    _pvideo.status = 'lost';
    _pvRender();
    return;
  }
  _pvSchedulePoll();
}

async function _pvPollOnce() {
  var tid = _pvideo.regenTaskId || _pvideo.taskId;
  if (!tid) return;
  try {
    var resp = await Api.motion.poll(tid, _pvideo.cursor);
    /* Abort raced this in-flight poll (see podcast.js): state was already
     *   reset by the abort path — never flip status back (pt_3cd6cd48). */
    if (_pvideo.regenTaskId ? (_pvideo.regenTaskId !== tid) : (_pvideo.taskId !== tid)) return;
    if (!resp || !resp.ok) { _pvPollFail(); return; }
    _pvideo.pollFails = 0;
    /* Only real events reset the liveness clock — see podcast.js for why an
       empty-but-successful poll must NOT count as worker activity. */
    if ((resp.events || []).length) _pvideo.lastEventAt = Date.now();
    _pvideo.cursor = resp.next_cursor != null ? resp.next_cursor : _pvideo.cursor;
    var phaseChanged = false;
    (resp.events || []).forEach(function(ev) {
      if (_pvConsumeEvent(ev)) phaseChanged = true;
    });
    if (resp.done) {
      if (_pvideo.regenTaskId) {
        // A scene regen finished — refresh the grid + bust the player cache.
        _pvideo.regenTaskId = '';
        _pvideo.regenSceneId = '';
        _pvLoadScenes(true);
        return;
      }
      if (resp.status === 'done') {
        _pvideo.result = resp.result || null;
        _pvideo._doneTaskId = _pvideo.taskId;
        _pvideo.status = 'done';
        _pvideo.taskId = '';
        _pvStopPolling();
        _pvRender();
        _pvLoadScenes();
      } else if (resp.status === 'aborted') {
        _pvideo.status = 'idle';
        _pvideo.taskId = '';
        _pvStopPolling();
        _pvRender();
      } else if (resp.error && resp.error.kind === 'worker_lost') {
        _pvideo.status = 'lost';
        _pvideo.taskId = '';
        _pvStopPolling();
        _pvRender();
      } else {
        _pvideo.status = 'error';
        _pvideo.errorText = (resp.error && resp.error.detail) ||
          (typeof resp.error === 'string' ? resp.error : '') ||
          _pvT('paper.videoFailed', 'Video generation failed');
        _pvideo.taskId = '';
        _pvStopPolling();
        _pvRender();
      }
      return;
    }
    if (phaseChanged) { _pvRender(); } else { _pvRenderProgress(); }
    _pvSchedulePoll();
  } catch (e) {
    console.warn('[Paper:Video] poll failed:', e);
    _pvPollFail();
  }
}

/** Start generation from the card's current selections. */
async function _videoGenerate(force) {
  if (!_pvideo.paperHash) return;
  var langSel = document.getElementById('videoLangSel');
  var voiceInp = document.getElementById('videoVoiceInp');
  var narrChk = document.getElementById('videoNarrChk');
  var burnChk = document.getElementById('videoBurnChk');
  var qualSel = document.getElementById('videoQualSel');
  _pvideo.lang = langSel ? langSel.value : _pvideo.lang;
  _pvideo.voice = voiceInp ? voiceInp.value.trim() : _pvideo.voice;
  _pvideo.narration = narrChk ? !!narrChk.checked : _pvideo.narration;
  _pvideo.burnIn = burnChk ? !!burnChk.checked : _pvideo.burnIn;
  _pvideo.quality = qualSel ? qualSel.value : _pvideo.quality;
  _pvideo.status = 'generating';
  _pvideo.progress = { done: 0, total: 0, phase: '' };
  _pvResetRun();
  _pvRender();
  var genHash = _pvideo.paperHash;
  try {
    var resp = await Api.paper.videoStart({
      paper_hash: _pvideo.paperHash,
      lang: _pvideo.lang, voice: _pvideo.voice,
      narration: _pvideo.narration, burn_in: _pvideo.burnIn,
      quality: _pvideo.quality, force: !!force,
    });
    /* Paper switched mid-start — the OLD paper's task must not attach here (pt_3cd6cd48). */
    if (_pvideo.paperHash !== genHash) return;
    if (resp && resp.report_required) {
      _pvideo.status = 'report_required';
      _pvRender();
      return;
    }
    if (resp && resp.ok && resp.task_id) {
      _pvideo.taskId = resp.task_id;
      _pvideo.cursor = 0;
      _pvRenderProgress();
      _pvSchedulePoll();
      return;
    }
    _pvideo.status = 'error';
    _pvideo.errorText = (resp && resp.error) ||
      _pvT('paper.videoFailed', 'Video generation failed');
    _pvRender();
  } catch (e) {
    console.warn('[Paper:Video] start failed:', e);
    _pvideo.status = 'error';
    _pvideo.errorText = String(e && e.message || e);
    _pvRender();
  }
}

async function _videoAbort() {
  if (_pvideo.taskId) {
    try { await Api.motion.abort(_pvideo.taskId); }
    catch (e) { console.warn('[Paper:Video] abort failed:', e); }
  }
  _pvStopPolling();
  _pvideo.taskId = '';
  _pvideo.status = 'idle';
  _pvRender();
}

/** Load the per-scene grid (idempotent; cacheBust refreshes previews). */
async function _pvLoadScenes(cacheBust) {
  var tid = _pvideo._doneTaskId || _pvideo.taskId || '';
  if (!tid) return;
  try {
    var resp = await Api.motion.scenes(tid);
    if (resp && resp.ok) {
      _pvideo.scenes = resp.scenes || [];
      _pvRenderSceneGrid(cacheBust ? Date.now() : 0);
    }
  } catch (e) {
    console.warn('[Paper:Video] scenes load failed:', e);
  }
}

/** Re-render one scene (keeps the rest of the video untouched). */
async function _videoRegenScene(sceneId) {
  var tid = _pvideo._doneTaskId || _pvideo.taskId;
  if (!tid || _pvideo.regenSceneId) return;
  _pvideo.regenSceneId = sceneId;
  _pvRenderSceneGrid(0);
  try {
    var resp = await Api.motion.regenScene(tid, sceneId);
    if (resp && resp.ok && resp.task_id) {
      _pvideo.regenTaskId = resp.task_id;
      _pvideo.cursor = 0;
      _pvSchedulePoll();
      return;
    }
    _pvideo.regenSceneId = '';
    _pvRenderSceneGrid(0);
  } catch (e) {
    console.warn('[Paper:Video] regen failed:', e);
    _pvideo.regenSceneId = '';
    _pvRenderSceneGrid(0);
  }
}

// ── Rendering ──

/* Film-strip glyph (SVG only, never emoji — §3.4). */
function _pvHeroIconSvg(kind) {
  if (kind === 'video') {
    return '<svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M7 4v16M17 4v16M2 9h5M2 15h5M17 9h5M17 15h5"/></svg>';
  }
  return '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
}

function _pvPhaseLabel(phase) {
  var map = {
    parse: 'paper.videoPhaseParse', storyboard: 'paper.videoPhaseStoryboard',
    narrate: 'paper.videoPhaseNarrate', compose: 'paper.videoPhaseCompose',
    render: 'paper.videoPhaseRender',
    concat: 'paper.videoPhaseConcat', mux: 'paper.videoPhaseMux',
    burn_in: 'paper.videoPhaseBurnIn', regen: 'paper.videoPhaseRegen',
  };
  var fallbacks = {
    parse: 'Parsing subtitles', storyboard: 'Storyboarding',
    narrate: 'Voicing scenes', compose: 'Composing scenes',
    render: 'Rendering scenes',
    concat: 'Joining scenes', mux: 'Mixing audio',
    burn_in: 'Burning subtitles', regen: 'Re-rendering scene',
  };
  return _pvT(map[phase] || '', fallbacks[phase] || phase || '');
}

function _pvRenderProgress() {
  var el = document.getElementById('videoProgressLine');
  if (!el) return;
  var p = _pvideo.progress;
  var label = _pvPhaseLabel(p.phase);
  var line = (p.total > 0)
    ? label + ' ' + p.done + '/' + p.total
    : (label || _pvT('paper.videoStarting', 'Starting…'));
  if (_pvideo.etaSec > 0 && (p.phase === 'render' || p.phase === 'narrate')) {
    line += ' · ' + _pvT('paper.mediaEtaPrefix', '≈') + _pvFmtSec(_pvideo.etaSec);
  }
  el.textContent = line;
}

function _pvFmtSec(x) {
  x = Math.max(0, Math.floor(x || 0));
  return Math.floor(x / 60) + ':' + ('0' + (x % 60)).slice(-2);
}

/** Phase stepper (P-UX2) — vocabulary comes from the server's phase_started. */
function _pvStepper() {
  var phases = _pvideo.phases.length ? _pvideo.phases :
    ['storyboard', 'narrate', 'compose', 'render', 'concat', 'mux'];
  var cur = Math.max(_pvideo.phaseIndex, 1);
  var h = '<div class="paper-stepper">';
  phases.forEach(function(ph, i) {
    var idx = i + 1;
    var state = idx < cur ? 'is-done' : (idx === cur ? 'is-active' : '');
    var mark = idx < cur ? '✓' : (idx === cur ? '●' : '○');
    h += '<span class="paper-step ' + state + '">' +
      '<span class="paper-step-mark">' + mark + '</span>' +
      _pvEsc(_pvPhaseLabel(ph)) + '</span>';
    if (idx < phases.length) h += '<span class="paper-step-sep"></span>';
  });
  return h + '</div>';
}

/** Liveness line (P-UX2): elapsed + last-activity; stale tint after 30s. */
function _pvRenderActivity() {
  var el = document.getElementById('videoActivityLine');
  if (!el || _pvideo.status !== 'generating') return;
  var elapsed = Math.max(0, Math.round((Date.now() - _pvideo.genStartedAt) / 1000));
  var quiet = Math.max(0, Math.round((Date.now() - _pvideo.lastEventAt) / 1000));
  el.classList.toggle('is-stale', quiet > 30);
  el.textContent = _pvT('paper.mediaElapsed', 'elapsed') + ' ' + _pvFmtSec(elapsed) +
    ' · ' + _pvT('paper.mediaLastActive', 'last activity') + ' ' + _pvFmtSec(quiet) +
    (quiet > 30 ? ' — ' + _pvT('paper.mediaStillRunning',
      'still running (this step can take minutes)') : '');
}

function _pvDegradeBanner() {
  return '<div class="paper-podcast-banner">' +
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
    '<span>' + _pvEsc(_pvT('paper.videoNoTts',
      'No TTS voice slot is configured — this run generates a silent video.')) +
    '</span></div>';
}

/* Option-card / segmented-control → hidden <select> bridge. Shared with
 * podcast.js (guarded — first definition in the bundle wins, both bodies
 * are identical). The real <select> keeps the generate path and the jsdom
 * contract (stable ids + .value); the cards write the pick back into it. */
if (typeof _pmPick !== 'function') {
  var _pmPick = function (btn) {
    var selId = btn.getAttribute('data-sel');
    var sel = document.getElementById(selId);
    if (!sel) return;
    sel.value = btn.getAttribute('data-value');
    var sibs = btn.parentNode
      ? btn.parentNode.querySelectorAll('[data-sel="' + selId + '"]') : [];
    for (var i = 0; i < sibs.length; i++) sibs[i].classList.remove('is-selected');
    btn.classList.add('is-selected');
  };
}

/* Studio icon set (SVG only, never emoji). */
function _pvIconSvg(name) {
  if (name === 'zap') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>';
  }
  if (name === 'gauge') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 19a9 9 0 1 1 16 0"/><path d="M12 15l3.5-5.5"/></svg>';
  }
  if (name === 'gem') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h12l4 6-10 12L2 9l4-6z"/><path d="M2 9h20" opacity=".6"/><path d="M9 3L7 9l5 12 5-12-2-6" opacity=".6"/></svg>';
  }
  if (name === 'mic') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><line x1="12" y1="17" x2="12" y2="21"/></svg>';
  }
  if (name === 'play') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M7 4.5v15l13-7.5z"/></svg>';
  }
  if (name === 'film') {
    return '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M7 4v16M17 4v16M2 9h5M2 15h5M17 9h5M17 15h5"/></svg>';
  }
  if (name === 'download') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
  }
  if (name === 'file') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
  }
  if (name === 'refresh') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>';
  }
  return '';
}

/** One rich option card bound to a hidden select (see _pmPick). */
function _pvOptCard(selId, value, icon, title, sub, selected) {
  return '<button type="button" class="pm-opt' + (selected ? ' is-selected' : '') +
    '" data-sel="' + selId + '" data-value="' + value + '" onclick="_pmPick(this)">' +
    '<span class="pm-opt-icon">' + _pvIconSvg(icon) + '</span>' +
    '<span class="pm-opt-title">' + _pvEsc(title) + '</span>' +
    '<span class="pm-opt-sub">' + _pvEsc(sub) + '</span></button>';
}

/** One segment of a segmented control bound to a hidden select. */
function _pvSegBtn(selId, value, label, selected) {
  return '<button type="button" class="pm-seg-btn' + (selected ? ' is-selected' : '') +
    '" data-sel="' + selId + '" data-value="' + value + '" onclick="_pmPick(this)">' +
    _pvEsc(label) + '</button>';
}

function _pvRender() {
  var host = _pvEl();
  if (!host) return;
  var s = _pvideo;
  var h = '';

  if (s.status === 'loading') {
    host.innerHTML = '<div class="paper-report-empty"><p>…</p></div>';
    return;
  }

  if (s.status === 'report_required') {
    host.innerHTML =
      '<div class="paper-podcast-hero">' +
      '<div class="paper-podcast-hero-icon">' + _pvHeroIconSvg('video') + '</div>' +
      '<div class="paper-podcast-hero-title">' +
      _pvEsc(_pvT('paper.videoHeroTitle', 'Watch this paper')) + '</div>' +
      '<div class="paper-podcast-hero-sub">' +
      _pvEsc(_pvT('paper.videoNeedReport',
        'The video abstract is adapted from the analysis report — generate the report first.')) + '</div>' +
      '<button class="paper-podcast-btn" onclick="_switchPaperTab(\'report\')">' +
      _pvEsc(_pvT('paper.videoGoReport', 'Go generate the report')) + '</button>' +
      '</div>';
    return;
  }

  if (s.status === 'lookup_failed') {
    host.innerHTML =
      '<div class="paper-podcast-hero">' +
      '<div class="paper-podcast-hero-icon is-warn">' + _pvHeroIconSvg('warn') + '</div>' +
      '<div class="paper-podcast-hero-sub">' + _pvEsc(s.errorText ||
        _pvT('paper.videoLookupFailed', 'Video status lookup failed — check the server log.')) + '</div>' +
      '<button class="paper-podcast-btn paper-podcast-btn-ghost" onclick="_initVideoTab(true)">' +
      _pvEsc(_pvT('paper.videoRetry', 'Retry')) + '</button>' +
      '</div>';
    return;
  }

  /* Studio console card (lang/quality/voice/toggles + generate CTA). The
   * rich option cards write into the hidden selects (_pmPick) — the
   * generate path still reads the selects, so the contract never leaves
   * the DOM. */
  if (s.status === 'idle' || s.status === 'error') {
    h += '<div class="paper-podcast-card pm-studio">';
    h += '<div class="pm-studio-head">' +
      '<div class="pm-studio-badge is-video">' + _pvHeroIconSvg('video') + '</div>' +
      '<div class="pm-studio-head-text">' +
      '<div class="pm-studio-title">' +
      _pvEsc(_pvT('paper.videoStudioTitle', 'Video studio')) + '</div>' +
      '<div class="pm-studio-sub">' + _pvEsc(_pvT('paper.videoHint',
        'A short narrated motion-graphic video of this paper — beats, charts and kinetic type.')) +
      '</div></div></div>';
    if (!s.ttsAvailable) h += _pvDegradeBanner();
    h += '<div class="pm-field"><div class="pm-field-label">' +
      _pvEsc(_pvT('paper.mediaOptLang', 'Language')) + '</div>' +
      '<div class="pm-seg">' +
      _pvSegBtn('videoLangSel', 'zh', '中文', s.lang === 'zh') +
      _pvSegBtn('videoLangSel', 'en', 'English', s.lang === 'en') +
      '</div>' +
      '<select id="videoLangSel" class="pm-sr" tabindex="-1" aria-hidden="true">' +
      '<option value="zh"' + (s.lang === 'zh' ? ' selected' : '') + '>中文</option>' +
      '<option value="en"' + (s.lang === 'en' ? ' selected' : '') + '>English</option></select></div>';
    h += '<div class="pm-field"><div class="pm-field-label">' +
      _pvEsc(_pvT('paper.mediaOptQuality', 'Quality')) + '</div>' +
      '<div class="pm-options cols-3">' +
      _pvOptCard('videoQualSel', 'draft', 'zap',
        _pvT('paper.videoQualityDraft', 'Draft (fast)'),
        _pvT('paper.videoQualityDraftSub', 'fast preview'), s.quality === 'draft') +
      _pvOptCard('videoQualSel', 'standard', 'gauge',
        _pvT('paper.videoQualityStandard', 'Standard'),
        _pvT('paper.videoQualityStandardSub', 'recommended'), s.quality === 'standard') +
      _pvOptCard('videoQualSel', 'high', 'gem',
        _pvT('paper.videoQualityHigh', 'High'),
        _pvT('paper.videoQualityHighSub', 'slower, finer'), s.quality === 'high') +
      '</div>' +
      '<select id="videoQualSel" class="pm-sr" tabindex="-1" aria-hidden="true">' +
      '<option value="draft"' + (s.quality === 'draft' ? ' selected' : '') + '>' +
      _pvEsc(_pvT('paper.videoQualityDraft', 'Draft (fast)')) + '</option>' +
      '<option value="standard"' + (s.quality === 'standard' ? ' selected' : '') + '>' +
      _pvEsc(_pvT('paper.videoQualityStandard', 'Standard')) + '</option>' +
      '<option value="high"' + (s.quality === 'high' ? ' selected' : '') + '>' +
      _pvEsc(_pvT('paper.videoQualityHigh', 'High')) + '</option></select></div>';
    h += '<div class="pm-field"><div class="pm-field-label">' +
      _pvEsc(_pvT('paper.mediaOptVoice', 'Voice')) +
      '<span class="pm-field-opt">' +
      _pvEsc(_pvT('paper.mediaOptional', 'optional')) + '</span></div>' +
      '<div class="pm-voice-wrap">' + _pvIconSvg('mic') +
      '<input id="videoVoiceInp" type="text" value="' +
      _pvEsc(s.voice) + '" placeholder="' + _pvEsc(s.defaultVoice ||
        _pvT('paper.podcastVoice', 'voice (optional)')) + '" /></div></div>';
    h += '<div class="pm-field"><div class="pm-field-label">' +
      _pvEsc(_pvT('paper.mediaOptExtras', 'Options')) + '</div>' +
      '<div class="pm-toggles">' +
      '<label class="pm-toggle">' +
      '<input id="videoNarrChk" type="checkbox"' + (s.narration ? ' checked' : '') + ' />' +
      '<span class="pm-toggle-track"><span class="pm-toggle-thumb"></span></span>' +
      '<span class="pm-toggle-text"><b>' +
      _pvEsc(_pvT('paper.videoNarration', 'Narration')) + '</b><small>' +
      _pvEsc(_pvT('paper.videoNarrationSub', 'TTS voice-over')) +
      '</small></span></label>' +
      '<label class="pm-toggle">' +
      '<input id="videoBurnChk" type="checkbox"' + (s.burnIn ? ' checked' : '') + ' />' +
      '<span class="pm-toggle-track"><span class="pm-toggle-thumb"></span></span>' +
      '<span class="pm-toggle-text"><b>' +
      _pvEsc(_pvT('paper.videoBurnIn', 'Burn-in subtitles')) + '</b><small>' +
      _pvEsc(_pvT('paper.videoBurnInSub', 'subtitles baked into the frame')) +
      '</small></span></label>' +
      '</div></div>';
    h += '<button class="paper-podcast-btn pm-cta" onclick="_videoGenerate()">' +
      _pvIconSvg('play') + '<span>' +
      _pvEsc(_pvT('paper.videoGenerate', 'Generate video')) + '</span></button>';
    if (s.status === 'error' && s.errorText) {
      h += '<div class="paper-podcast-error">' + _pvEsc(s.errorText) + '</div>';
    }
    h += '</div>';
    host.innerHTML = h;
    return;
  }

  if (s.status === 'generating') {
    h += '<div class="paper-podcast-card pm-console">';
    h += '<div class="pm-console-head">' +
      '<span class="pm-clap" aria-hidden="true">' + _pvIconSvg('film') + '</span>' +
      '<span class="pm-console-title">' +
      _pvEsc(_pvT('paper.videoMakingTitle', 'Producing your video')) + '</span>' +
      '<button class="paper-podcast-btn paper-podcast-btn-ghost pm-console-abort" onclick="_videoAbort()">' +
      _pvEsc(_pvT('paper.podcastAbort', 'Abort')) + '</button></div>';
    h += _pvStepper();
    h += '<div class="paper-podcast-progress">';
    h += '<span class="paper-podcast-spinner"></span>';
    h += '<span id="videoProgressLine">' +
      _pvEsc(_pvT('paper.videoStarting', 'Starting…')) + '</span>';
    h += '</div>';
    h += '<div class="pm-renderbar" aria-hidden="true"></div>';
    h += '<div class="paper-media-activity" id="videoActivityLine"></div>';
    // P-UX3: the grid fills in scene-by-scene as scene_done events land.
    h += '<div class="paper-video-grid" id="paperVideoGrid"></div>';
    h += '</div>';
    host.innerHTML = h;
    _pvRenderProgress();
    _pvRenderActivity();
    _pvStartTick();
    _pvRenderSceneGrid(0);
    return;
  }

  /* P-UX1/P-UX4 terminal honest states. */
  if (s.status === 'lost' || s.status === 'interrupted') {
    var lost = s.status === 'lost';
    host.innerHTML =
      '<div class="paper-podcast-hero">' +
      '<div class="paper-podcast-hero-icon is-warn">' + _pvHeroIconSvg('warn') + '</div>' +
      '<div class="paper-podcast-hero-sub">' + _pvEsc(lost
        ? _pvT('paper.podcastLost', 'Task lost or connection dropped — the generation task can no longer be reached.')
        : _pvT('paper.podcastInterrupted', 'The last generation was cut short by a server restart.')) + '</div>' +
      '<div class="paper-podcast-actions">' +
      '<button class="paper-podcast-btn paper-podcast-btn-ghost" onclick="_initVideoTab(true)">' +
      _pvEsc(_pvT('paper.podcastRecheck', 'Re-check status')) + '</button>' +
      '<button class="paper-podcast-btn" onclick="_videoGenerate(true)">' +
      _pvEsc(_pvT('paper.podcastRegenerate', 'Regenerate')) + '</button>' +
      '</div></div>';
    return;
  }

  // done
  var r = s.result || {};
  var tid = s._doneTaskId || '';
  var fileUrl = tid ? Api.motion.fileUrl(tid) : '';
  h += '<div class="paper-podcast-card pm-studio">';
  h += '<div class="paper-podcast-head">';
  h += '<span class="paper-podcast-title">' +
    _pvEsc(_pvT('paper.videoHeroTitle', 'Watch this paper')) + '</span>';
  h += '<span class="paper-podcast-badge">' + _pvEsc(s.quality) + ' · ' +
    _pvEsc(s.lang) + '</span>';
  if (r.duration) {
    var mm = Math.floor(r.duration / 60), ss = Math.round(r.duration % 60);
    h += '<span class="paper-podcast-badge">' + mm + ':' +
      (ss < 10 ? '0' : '') + ss + '</span>';
  }
  if (r.narrated === false) {
    h += '<span class="paper-podcast-badge">' +
      _pvEsc(_pvT('paper.videoSilent', 'silent')) + '</span>';
  }
  h += '</div>';
  if (fileUrl) {
    h += '<video id="paperVideoPlayer" class="paper-video-player" controls ' +
      'preload="metadata" src="' + _pvEsc(fileUrl) + '"></video>';
    h += '<div class="paper-podcast-actions">';
    h += '<a class="paper-podcast-btn" href="' + _pvEsc(fileUrl) +
      '" download="paper-video-' + (s.paperHash || '').slice(0, 8) + '.mp4">' +
      _pvIconSvg('download') + '<span>' +
      _pvEsc(_pvT('paper.videoDownload', 'Download video')) + '</span></a>';
    h += '<a class="paper-podcast-btn paper-podcast-btn-ghost" href="' +
      _pvEsc(Api.motion.fileUrl(tid, 'srt')) + '" download="paper-video-' +
      (s.paperHash || '').slice(0, 8) + '.srt">' + _pvIconSvg('file') +
      '<span>' + _pvEsc(_pvT('paper.videoDownloadSrt', 'Download SRT')) + '</span></a>';
    h += '<button class="paper-podcast-btn paper-podcast-btn-ghost" onclick="_videoGenerate(true)">' +
      _pvIconSvg('refresh') + '<span>' +
      _pvEsc(_pvT('paper.podcastRegenerate', 'Regenerate')) + '</span></button>';
    h += '</div>';
  }
  h += '<div class="paper-video-grid" id="paperVideoGrid"></div>';
  h += '</div>';
  host.innerHTML = h;
  _pvRenderSceneGrid(0);
}

/** Scene grid: per-scene preview (its own mp4) + regen button. */
function _pvRenderSceneGrid(cacheBust) {
  var grid = document.getElementById('paperVideoGrid');
  if (!grid) return;
  var s = _pvideo;
  var tid = s._doneTaskId || s.taskId || '';
  if (!tid || !(s.scenes || []).length) { grid.innerHTML = ''; return; }
  var h = '<div class="paper-video-grid-title">' +
    _pvEsc(_pvT('paper.videoScenesTitle', 'Scenes — preview or re-render one')) +
    '</div><div class="paper-video-grid-row">';
  var generating = s.status === 'generating';
  s.scenes.forEach(function(sc) {
    var sid = sc.scene_id;
    var regening = s.regenSceneId === sid;
    var src = Api.motion.sceneFileUrl(tid, sid) +
      (cacheBust ? '?v=' + cacheBust : '');
    h += '<div class="paper-video-cell' + (regening ? ' is-regening' : '') +
      (generating && !sc.has_video ? ' is-pending' : '') + '">';
    if (sc.has_video) {
      h += '<video class="paper-video-thumb" preload="metadata" muted ' +
        'src="' + _pvEsc(src) + '"' +
        ' onclick="this.paused?this.play():this.pause()"></video>';
    } else {
      h += '<div class="paper-video-thumb paper-video-thumb-empty">…</div>';
    }
    h += '<div class="paper-video-cell-text" title="' + _pvEsc(sc.text || '') + '">' +
      _pvEsc((sc.text || '').slice(0, 42)) + '</div>';
    if (!generating) {
      h += '<button class="paper-video-regen" data-scene="' + _pvEsc(sid) + '"' +
        (regening ? ' disabled' : '') +
        ' onclick="_videoRegenScene(\'' + _pvEsc(sid) + '\')">' +
        (regening ? _pvEsc(_pvT('paper.videoRegening', 'Re-rendering…'))
                  : _pvEsc(_pvT('paper.videoRegen', 'Re-render'))) + '</button>';
    }
    h += '</div>';
  });
  h += '</div>';
  grid.innerHTML = h;
}
