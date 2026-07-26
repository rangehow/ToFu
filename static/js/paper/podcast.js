/* ═══════════════════════════════════════════
   paper/podcast.js — Paper Podcast tab

   Turns a paper report into a listenable solo podcast
   (docs/PAPER_PODCAST_DESIGN.md). Server owns the task
   (/api/v1/paper/podcast/*); this module only renders + polls —
   same transport shape as the report tab beside it.

   States rendered into #paperPodcastContent:
     idle / generating (progress) / done (player + transcript)
     script_only (no TTS slot → script + transcript, honest banner)
     report_required (chain to the Report tab) / error
   ═══════════════════════════════════════════ */

// ── State ──
var _podcast = {
  paperHash: '',
  mode: 'short',
  lang: 'zh',
  voice: '',
  taskId: '',
  cursor: 0,
  pollTimer: null,
  status: 'idle',          // idle|generating|done|script_only|report_required|lookup_failed|lost|interrupted|error
  data: null,              // {script, meta, audioUrl, durationSec, scriptOnly}
  errorText: '',
  progress: { done: 0, total: 0 },
  ttsAvailable: true,
  defaultVoice: '',
  sleepTimerId: 0,
  sleepDeadline: 0,
  // P-UX progress perception (docs/PAPER_MEDIA_UX_DESIGN.md §3.4)
  pollFails: 0,            // consecutive poll failures → 5 = lost state
  phases: [],              // server phase vocabulary (phase_started.phases)
  phaseIndex: 0,           // 1-based index of the current phase
  currentPhase: '',
  scriptStep: '',          // draft|validate|revise|critic (script sub-step)
  scriptChars: 0,          // chars streamed so far in the current draft pass
  scriptSegments: 0,       // segments started so far (counted from the stream)
  scriptCharTarget: 0,     // the char target the prompt actually instructed
  genStartedAt: 0,         // local stopwatch start
  lastEventAt: 0,          // last event/poll-success time (liveness)
  tickTimer: null,         // 1s UI ticker for elapsed/last-active
  _segFirstTick: 0,        // wall-clock of the first segment_done (ETA)
  etaSec: 0,
};
// Poll cadence — a var (not const) so the JSDOM harness can shrink it.
var _PODCAST_POLL_MS = 1200;
// Consecutive poll failures before the honest 'lost' terminal state (拍板 A).
var _PC_POLL_FAIL_LIMIT = 5;

function _pcResetRun() {
  _podcast.pollFails = 0;
  _podcast.phases = [];
  _podcast.phaseIndex = 0;
  _podcast.currentPhase = '';
  _podcast.scriptStep = '';
  _podcast.scriptChars = 0;
  _podcast.scriptSegments = 0;
  _podcast.scriptCharTarget = 0;
  _podcast.genStartedAt = Date.now();
  _podcast.lastEventAt = Date.now();
  _podcast._segFirstTick = 0;
  _podcast.etaSec = 0;
}

function _pcT(key, fallback) {
  return (typeof t === 'function') ? t(key) : (fallback || key);
}

function _pcEl() { return document.getElementById('paperPodcastContent'); }

function _pcEsc(s) {
  return (typeof escapeHtml === 'function') ? escapeHtml(s == null ? '' : s)
    : String(s == null ? '' : s);
}

/* Stop the poll timer ONLY.
 *
 * The 1s activity ticker is deliberately NOT stopped here: _pcSchedulePoll()
 * calls this before arming the next poll, so folding the ticker in would kill
 * the elapsed/last-activity stopwatch on the FIRST poll and freeze the line
 * at 0:00 for the rest of the run — exactly the "looks stuck" symptom the
 * liveness line exists to prevent. Terminal states call _pcStopPolling()
 * instead, which stops both. */
function _pcStopPoll() {
  if (_podcast.pollTimer) { clearTimeout(_podcast.pollTimer); _podcast.pollTimer = null; }
}

/** Terminal teardown: stop polling AND the ticker (run is over). */
function _pcStopPolling() {
  _pcStopPoll();
  _pcStopTick();
}

function _pcStartTick() {
  if (_podcast.tickTimer) return;
  _podcast.tickTimer = setInterval(_pcRenderActivity, 1000);
}

function _pcStopTick() {
  if (_podcast.tickTimer) { clearInterval(_podcast.tickTimer); _podcast.tickTimer = null; }
}

/** Entry point — called by _switchPaperTab('podcast'). Renders the tab by
 * looking the paper up server-side: a live task re-attaches, a cached
 * podcast renders instantly, otherwise the generate card shows. */
async function _initPodcastTab(force) {
  var host = _pcEl();
  if (!host) return;
  _pcStopPolling();
  _podcast.paperHash = (typeof _paperHash !== 'undefined') ? (_paperHash || '') : '';
  if (!_podcast.paperHash) {
    _podcast.status = 'idle';
    host.innerHTML = '<div class="paper-report-empty"><p>' +
      _pcEsc(_pcT('paper.reportNoText', 'No paper text available. Load a PDF first.')) +
      '</p></div>';
    return;
  }
  _podcast.status = 'loading';
  _pcRender();   // skeleton while the lookup round-trips
  try {
    // Feature status (TTS availability) + per-paper lookup ride together.
    var st = await Api.paper.podcastStatus();
    if (st && st.ok) {
      _podcast.ttsAvailable = !!st.tts_available;
      _podcast.defaultVoice = st.default_voice || '';
    }
    var look = await Api.paper.podcastLookup({
      paper_hash: _podcast.paperHash,
      mode: _podcast.mode, lang: _podcast.lang,
    });
    if (look && look.ok && look.found && look.running) {
      _podcast.taskId = look.task_id;
      _podcast.cursor = 0;
      _podcast.status = 'generating';
      _pcResetRun();
      _pcRender();
      _pcSchedulePoll();
      return;
    }
    // P-UX4: the previous run was cut by a server restart — honest state.
    if (look && look.ok && look.found && look.interrupted) {
      _podcast.status = 'interrupted';
      _pcRender();
      return;
    }
    if (look && look.ok && look.found && look.cached) {
      _podcast.data = look;
      _podcast.status = look.scriptOnly ? 'script_only' : 'done';
      _pcRender();
      return;
    }
    if (look && look.ok) {
      _podcast.reportAvailable = !!look.report_available;
      _podcast.status = _podcast.reportAvailable ? 'idle' : 'report_required';
    } else {
      /* ★ FIX (2026-07-25): a FAILED lookup (server 5xx with onError:'null'
       * → null — e.g. the missing paper_podcasts table that 500'd every call)
       * used to fall through to report_required, telling the user "generate a
       * report first" for a paper that HAD one — and chaining to the Report
       * tab could never fix it. report_required is now derived ONLY from an
       * ok response; a failed lookup gets its own honest state with a retry. */
      _podcast.status = 'lookup_failed';
      _podcast.errorText = _pcT('paper.podcastLookupFailed',
        'Podcast status lookup failed — check the server log.');
    }
    _pcRender();
  } catch (e) {
    console.warn('[Paper:Podcast] lookup failed:', e);
    _podcast.status = 'lookup_failed';
    _podcast.errorText = String(e && e.message || e);
    _pcRender();
  }
}

function _pcSchedulePoll() {
  _pcStopPoll();
  _podcast.pollTimer = setTimeout(_pcPollOnce, _PODCAST_POLL_MS);
}

function _pcConsumeEvent(ev) {
  if (ev.type === 'phase_started') {
    _podcast.phases = ev.phases || _podcast.phases;
    _podcast.phaseIndex = ev.phase_index || 0;
    _podcast.currentPhase = ev.phase || '';
    if (ev.phase !== 'audio') { _podcast._segFirstTick = 0; _podcast.etaSec = 0; }
    return true;   // phase changed → the stepper needs a full re-render
  } else if (ev.type === 'progress' && ev.phase === 'script') {
    _podcast.scriptStep = ev.step || '';
    /* The draft pass streams: these counters are MEASURED (chars emitted,
       segments started), so they advance during the 1-3 min LLM call that
       otherwise shows a frozen label. A restarted attempt re-sends from
       scratch and reports 0 — assign, never accumulate. */
    if (typeof ev.chars === 'number') _podcast.scriptChars = ev.chars;
    if (typeof ev.segments === 'number') _podcast.scriptSegments = ev.segments;
    if (typeof ev.char_target === 'number') _podcast.scriptCharTarget = ev.char_target;
  } else if (ev.type === 'segment_done') {
    _podcast.progress = { done: ev.done, total: ev.total };
    // Honest ETA (拍板 A): wall-clock rate of the segments done so far.
    var now = Date.now();
    if (!_podcast._segFirstTick) _podcast._segFirstTick = now;
    if (ev.done > 0 && ev.total > ev.done) {
      _podcast.etaSec = Math.round(
        (now - _podcast._segFirstTick) / 1000 / ev.done * (ev.total - ev.done));
    } else {
      _podcast.etaSec = 0;
    }
  }
  // 'heartbeat' needs no handling — any event already bumps lastEventAt.
}

/** P-UX1 terminal backstop: a poll that keeps failing (404 after a server
 * restart, network down) must NOT spin forever — 5 strikes → lost state. */
function _pcPollFail() {
  _podcast.pollFails++;
  if (_podcast.pollFails >= _PC_POLL_FAIL_LIMIT) {
    _pcStopPolling();
    _podcast.taskId = '';
    _podcast.status = 'lost';
    _pcRender();
    return;
  }
  _pcSchedulePoll();
}

async function _pcPollOnce() {
  if (!_podcast.taskId) return;
  try {
    var resp = await Api.paper.podcastPoll(_podcast.taskId, _podcast.cursor);
    if (!resp || !resp.ok) { _pcPollFail(); return; }
    _podcast.pollFails = 0;
    /* Liveness is about the WORKER, not the HTTP round-trip: a successful poll
       that carries zero events proves only that the server answers. Bumping
       lastEventAt here pinned "last activity" at 0:00 forever and made the
       >30s stale tint unreachable — a silent worker looked identical to a busy
       one. Only real events (incl. the 10s worker heartbeat) reset the clock. */
    if ((resp.events || []).length) _podcast.lastEventAt = Date.now();
    _podcast.cursor = resp.cursor || _podcast.cursor;
    var phaseChanged = false;
    (resp.events || []).forEach(function(ev) {
      if (_pcConsumeEvent(ev)) phaseChanged = true;
    });
    _podcast.progress = resp.progress || _podcast.progress;
    if (resp.done) {
      if (resp.status === 'done') {
        _podcast.data = resp;
        _podcast.status = resp.scriptOnly ? 'script_only' : 'done';
      } else if (resp.status === 'aborted') {
        _podcast.status = 'idle';
      } else if (resp.error && resp.error.kind === 'worker_lost') {
        // P-UX1: server reaped a dead worker — same honest lost state.
        _podcast.status = 'lost';
      } else {
        _podcast.status = 'error';
        _podcast.errorText = (resp.error && resp.error.detail) ||
          (typeof resp.error === 'string' ? resp.error : '') ||
          _pcT('paper.podcastFailed', 'Podcast generation failed');
      }
      _podcast.taskId = '';
      _pcStopPolling();   // stops the poll timer AND the 1s activity ticker
      _pcRender();
      return;
    }
    if (phaseChanged) { _pcRender(); } else { _pcRenderProgress(); }
    _pcSchedulePoll();
  } catch (e) {
    console.warn('[Paper:Podcast] poll failed:', e);
    _pcPollFail();
  }
}

/** Start (or restart) generation from the card's current selections. */
async function _podcastGenerate(force) {
  if (!_podcast.paperHash) return;
  var modeSel = document.getElementById('podcastModeSel');
  var langSel = document.getElementById('podcastLangSel');
  var voiceInp = document.getElementById('podcastVoiceInp');
  _podcast.mode = modeSel ? modeSel.value : _podcast.mode;
  _podcast.lang = langSel ? langSel.value : _podcast.lang;
  _podcast.voice = voiceInp ? voiceInp.value.trim() : _podcast.voice;
  _podcast.status = 'generating';
  _podcast.progress = { done: 0, total: 0 };
  _pcResetRun();
  _pcRender();
  try {
    var resp = await Api.paper.podcastStart({
      paper_hash: _podcast.paperHash,
      mode: _podcast.mode, lang: _podcast.lang,
      voice: _podcast.voice, force: !!force,
    });
    if (resp && resp.report_required) {
      _podcast.status = 'report_required';
      _pcRender();
      return;
    }
    if (resp && resp.ok && resp.cached) {
      _podcast.data = resp;
      _podcast.status = resp.scriptOnly ? 'script_only' : 'done';
      _pcRender();
      return;
    }
    if (resp && resp.ok && resp.task_id) {
      _podcast.taskId = resp.task_id;
      _podcast.cursor = 0;
      _pcRenderProgress();
      _pcSchedulePoll();
      return;
    }
    _podcast.status = 'error';
    _podcast.errorText = (resp && resp.error) ||
      _pcT('paper.podcastFailed', 'Podcast generation failed');
    _pcRender();
  } catch (e) {
    console.warn('[Paper:Podcast] start failed:', e);
    _podcast.status = 'error';
    _podcast.errorText = String(e && e.message || e);
    _pcRender();
  }
}

async function _podcastAbort() {
  if (_podcast.taskId) { try { await Api.paper.podcastAbort(_podcast.taskId); } catch (e) { console.warn('[Paper:Podcast] abort failed:', e); } }
  _pcStopPolling();
  _podcast.taskId = '';
  _podcast.status = 'idle';
  _pcRender();
}

// ── Rendering ──

/* Hero icons — headphones (same glyph as the Podcast tab button) and the
 * warning triangle (same as the degrade banner). SVG only, never emoji (§3.4). */
function _pcHeroIconSvg(kind) {
  if (kind === 'podcast') {
    return '<svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>';
  }
  return '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
}

function _pcRenderProgress() {
  var el = document.getElementById('podcastProgressLine');
  if (!el) return;
  var p = _podcast.progress;
  var line;
  if (p.total > 0) {
    line = _pcT('paper.podcastAudioPhase', 'Synthesizing audio') + ' ' +
      p.done + '/' + p.total;
    if (_podcast.etaSec > 0) {
      line += ' · ' + _pcT('paper.mediaEtaPrefix', '≈') + _pcFmtSec(_podcast.etaSec);
    }
  } else {
    line = _pcT('paper.podcastScriptPhase', 'Writing the spoken script…');
    var stepMap = { draft: 'paper.podcastStepDraft', validate: 'paper.podcastStepValidate',
                    revise: 'paper.podcastStepRevise', critic: 'paper.podcastStepCritic' };
    var fallback = { draft: 'draft done', validate: 'checking quality',
                     revise: 'revising', critic: 'editor review' };
    /* During the draft pass the counters stream in, so show what has really
     * been written instead of a label that cannot change for 1-3 minutes.
     * Only measured numbers: segments started, and chars against the target
     * the prompt instructed. No invented percentage or segment denominator —
     * the prompt bounds total LENGTH, never a segment count. */
    if (_podcast.scriptChars > 0 &&
        (_podcast.scriptStep === 'draft' || _podcast.scriptStep === 'revise')) {
      if (_podcast.scriptStep === 'revise') {
        line += ' · ' + _pcT(stepMap.revise, fallback.revise);
      }
      if (_podcast.scriptSegments > 0) {
        line += ' · ' + _pcT('paper.podcastStreamSegments', 'segment') + ' ' +
          _podcast.scriptSegments;
      }
      line += ' · ' + _podcast.scriptChars +
        (_podcast.scriptCharTarget > 0 ? '/~' + _podcast.scriptCharTarget : '') +
        ' ' + _pcT('paper.podcastStreamChars', 'chars');
    } else if (_podcast.scriptStep) {
      line += ' · ' + _pcT(stepMap[_podcast.scriptStep] || '',
                          fallback[_podcast.scriptStep] || _podcast.scriptStep);
    }
  }
  el.textContent = line;
}

/** Phase stepper (P-UX2): 素材 → 剧本 → 配音, done ✓ / active ● / todo ○. */
function _pcStepper() {
  var phases = _podcast.phases.length ? _podcast.phases : ['source', 'script', 'audio'];
  var labelMap = { source: ['paper.podcastPhaseSource', 'Material'],
                   script: ['paper.podcastPhaseScript', 'Script'],
                   audio: ['paper.podcastPhaseAudio', 'Voice-over'] };
  var cur = Math.max(_podcast.phaseIndex, 1);
  var h = '<div class="paper-stepper">';
  phases.forEach(function(ph, i) {
    var idx = i + 1;
    var state = idx < cur ? 'is-done' : (idx === cur ? 'is-active' : '');
    var mark = idx < cur ? '✓' : (idx === cur ? '●' : '○');
    var lab = labelMap[ph] || ['', ph];
    h += '<span class="paper-step ' + state + '">' +
      '<span class="paper-step-mark">' + mark + '</span>' +
      _pcEsc(_pcT(lab[0], lab[1])) + '</span>';
    if (idx < phases.length) h += '<span class="paper-step-sep"></span>';
  });
  return h + '</div>';
}

/** Liveness line (P-UX2): elapsed stopwatch + "last activity Xs ago";
 * goes visibly stale after 30s of silence (quiet ≠ dead). */
function _pcRenderActivity() {
  var el = document.getElementById('podcastActivityLine');
  if (!el || _podcast.status !== 'generating') return;
  var elapsed = Math.max(0, Math.round((Date.now() - _podcast.genStartedAt) / 1000));
  var quiet = Math.max(0, Math.round((Date.now() - _podcast.lastEventAt) / 1000));
  var txt = _pcT('paper.mediaElapsed', 'elapsed') + ' ' + _pcFmtSec(elapsed) +
    ' · ' + _pcT('paper.mediaLastActive', 'last activity') + ' ' +
    _pcFmtSec(quiet) + '';
  el.classList.toggle('is-stale', quiet > 30);
  el.textContent = txt + (quiet > 30 ? ' — ' +
    _pcT('paper.mediaStillRunning', 'still running (this step can take minutes)') : '');
}

function _pcDegradeBanner() {
  return '<div class="paper-podcast-banner">' +
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
    '<span>' + _pcEsc(_pcT('paper.podcastNoTts',
      'No TTS voice slot is configured — this run generates the script + transcript only.')) +
    '</span></div>';
}

function _pcRender() {
  var host = _pcEl();
  if (!host) return;
  var s = _podcast;
  var h = '';

  if (s.status === 'loading') {
    host.innerHTML = '<div class="paper-report-empty"><p>…</p></div>';
    return;
  }

  if (s.status === 'report_required') {
    host.innerHTML =
      '<div class="paper-podcast-hero">' +
      '<div class="paper-podcast-hero-icon">' + _pcHeroIconSvg('podcast') + '</div>' +
      '<div class="paper-podcast-hero-title">' +
      _pcEsc(_pcT('paper.podcastHeroTitle', 'Listen to this paper')) + '</div>' +
      '<div class="paper-podcast-hero-sub">' +
      _pcEsc(_pcT('paper.podcastNeedReport',
        'The podcast is adapted from the analysis report — generate the report first.')) + '</div>' +
      '<div class="paper-podcast-hero-steps">' +
      '<span class="paper-podcast-hero-step is-active">' +
      _pcEsc(_pcT('paper.podcastStepReport', '1. Generate the report')) + '</span>' +
      '<span class="paper-podcast-hero-arrow">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg></span>' +
      '<span class="paper-podcast-hero-step">' +
      _pcEsc(_pcT('paper.podcastStepPodcast', '2. Adapt into a podcast')) + '</span>' +
      '</div>' +
      '<button class="paper-podcast-btn" onclick="_switchPaperTab(\'report\')">' +
      _pcEsc(_pcT('paper.podcastGoReport', 'Go generate the report')) + '</button>' +
      '</div>';
    return;
  }

  /* Lookup failure (server error / unreachable) — honest state with a retry;
   * deliberately NOT report_required: a 5xx proves nothing about the report. */
  if (s.status === 'lookup_failed') {
    host.innerHTML =
      '<div class="paper-podcast-hero">' +
      '<div class="paper-podcast-hero-icon is-warn">' + _pcHeroIconSvg('warn') + '</div>' +
      '<div class="paper-podcast-hero-sub">' + _pcEsc(s.errorText ||
        _pcT('paper.podcastLookupFailed', 'Podcast status lookup failed — check the server log.')) + '</div>' +
      '<button class="paper-podcast-btn paper-podcast-btn-ghost" onclick="_initPodcastTab(true)">' +
      _pcEsc(_pcT('paper.podcastRetry', 'Retry')) + '</button>' +
      '</div>';
    return;
  }

  // Card header (mode/lang/voice pickers + generate button) is always
  // available in idle/done/script_only/error so a re-roll is one click.
  if (s.status === 'idle' || s.status === 'error') {
    h += '<div class="paper-podcast-card">';
    h += '<div class="paper-podcast-hint">' + _pcEsc(_pcT('paper.podcastHint',
      'A solo spoken deep-read of this paper — for the commute or before sleep.')) + '</div>';
    if (!s.ttsAvailable) h += _pcDegradeBanner();
    h += '<div class="paper-podcast-form">';
    h += '<select id="podcastModeSel" class="paper-podcast-sel">' +
      '<option value="short"' + (s.mode === 'short' ? ' selected' : '') + '>' +
      _pcEsc(_pcT('paper.podcastModeShort', 'Short · ~5 min')) + '</option>' +
      '<option value="full"' + (s.mode === 'full' ? ' selected' : '') + '>' +
      _pcEsc(_pcT('paper.podcastModeFull', 'Full · ~15 min')) + '</option></select>';
    h += '<select id="podcastLangSel" class="paper-podcast-sel">' +
      '<option value="zh"' + (s.lang === 'zh' ? ' selected' : '') + '>中文</option>' +
      '<option value="en"' + (s.lang === 'en' ? ' selected' : '') + '>English</option></select>';
    h += '<input id="podcastVoiceInp" class="paper-podcast-voice" type="text" value="' +
      _pcEsc(s.voice) + '" placeholder="' + _pcEsc(s.defaultVoice ||
        _pcT('paper.podcastVoice', 'voice (optional)')) + '" />';
    h += '<button class="paper-podcast-btn" onclick="_podcastGenerate()">' +
      _pcEsc(_pcT('paper.podcastGenerate', 'Generate podcast')) + '</button>';
    h += '</div>';
    if (s.status === 'error' && s.errorText) {
      h += '<div class="paper-podcast-error">' + _pcEsc(s.errorText) + '</div>';
    }
    h += '</div>';
    host.innerHTML = h;
    return;
  }

  if (s.status === 'generating') {
    h += '<div class="paper-podcast-card">';
    h += _pcStepper();
    h += '<div class="paper-podcast-progress">';
    h += '<span class="paper-podcast-spinner"></span>';
    h += '<span id="podcastProgressLine">' +
      _pcEsc(_pcT('paper.podcastScriptPhase', 'Writing the spoken script…')) + '</span>';
    h += '<button class="paper-podcast-btn paper-podcast-btn-ghost" onclick="_podcastAbort()">' +
      _pcEsc(_pcT('paper.podcastAbort', 'Abort')) + '</button>';
    h += '</div>';
    h += '<div class="paper-media-activity" id="podcastActivityLine"></div>';
    h += '</div>';
    host.innerHTML = h;
    _pcRenderProgress();
    _pcRenderActivity();
    _pcStartTick();
    return;
  }

  /* P-UX1/P-UX4 terminal honest states. */
  if (s.status === 'lost' || s.status === 'interrupted') {
    var lost = s.status === 'lost';
    host.innerHTML =
      '<div class="paper-podcast-hero">' +
      '<div class="paper-podcast-hero-icon is-warn">' + _pcHeroIconSvg('warn') + '</div>' +
      '<div class="paper-podcast-hero-sub">' + _pcEsc(lost
        ? _pcT('paper.podcastLost', 'Task lost or connection dropped — the generation task can no longer be reached.')
        : _pcT('paper.podcastInterrupted', 'The last generation was cut short by a server restart.')) + '</div>' +
      '<div class="paper-podcast-actions">' +
      '<button class="paper-podcast-btn paper-podcast-btn-ghost" onclick="_initPodcastTab(true)">' +
      _pcEsc(_pcT('paper.podcastRecheck', 'Re-check status')) + '</button>' +
      '<button class="paper-podcast-btn" onclick="_podcastGenerate(true)">' +
      _pcEsc(_pcT('paper.podcastRegenerate', 'Regenerate')) + '</button>' +
      '</div></div>';
    return;
  }

  // done / script_only
  var d = s.data || {};
  var script = d.script || {};
  var meta = d.meta || {};
  var segs = script.segments || [];
  var scriptOnly = s.status === 'script_only';
  var audioUrl = d.audioUrl || '';
  var ext = (meta.container === 'wav') ? 'wav' : (meta.container === 'mp3' ? 'mp3' : 'bin');
  var dlName = 'paper-podcast-' + s.mode + '-' + (s.paperHash || '').slice(0, 8) + '.' + ext;

  h += '<div class="paper-podcast-card">';
  if (scriptOnly) h += _pcDegradeBanner();
  h += '<div class="paper-podcast-head">';
  h += '<span class="paper-podcast-title">' + _pcEsc(script.title || '') + '</span>';
  h += '<span class="paper-podcast-badge">' + _pcEsc(s.mode) + ' · ' + _pcEsc(s.lang) + '</span>';
  if (d.durationSec) {
    var mm = Math.floor(d.durationSec / 60), ss = Math.round(d.durationSec % 60);
    h += '<span class="paper-podcast-badge">' + mm + ':' + (ss < 10 ? '0' : '') + ss +
      (meta.duration_estimated ? '≈' : '') + '</span>';
  }
  h += '</div>';
  if (meta.low_confidence) {
    h += '<div class="paper-podcast-banner paper-podcast-banner-warn">' +
      _pcEsc(_pcT('paper.podcastLowConfidence',
        'QA gates did not fully pass — some content may be imprecise.')) + '</div>';
  }

  if (!scriptOnly && audioUrl) {
    h += '<audio id="podcastAudio" controls preload="metadata" src="' +
      _pcEsc(audioUrl) + '"></audio>';
    h += '<div class="paper-podcast-actions">';
    h += '<a class="paper-podcast-btn" href="' + _pcEsc(audioUrl) +
      '" download="' + _pcEsc(dlName) + '">' +
      _pcEsc(_pcT('paper.podcastDownloadAudio', 'Download audio')) + '</a>';
    h += '<label class="paper-podcast-sleep">' +
      _pcEsc(_pcT('paper.podcastSleepTimer', 'Sleep timer')) + ' ' +
      '<select id="podcastSleepSel" class="paper-podcast-sel" onchange="_podcastSleepTimerChange()">' +
      '<option value="0">' + _pcEsc(_pcT('paper.podcastSleepOff', 'Off')) + '</option>' +
      '<option value="5">5 ' + _pcEsc(_pcT('paper.podcastSleepMin', 'min')) + '</option>' +
      '<option value="10">10 ' + _pcEsc(_pcT('paper.podcastSleepMin', 'min')) + '</option>' +
      '<option value="15">15 ' + _pcEsc(_pcT('paper.podcastSleepMin', 'min')) + '</option>' +
      '<option value="30">30 ' + _pcEsc(_pcT('paper.podcastSleepMin', 'min')) + '</option>' +
      '<option value="45">45 ' + _pcEsc(_pcT('paper.podcastSleepMin', 'min')) + '</option>' +
      '</select><span id="podcastSleepNote" class="paper-podcast-sleep-note"></span></label>';
    h += '</div>';
  }

  h += '<div class="paper-podcast-actions">';
  h += '<button class="paper-podcast-btn paper-podcast-btn-ghost" onclick="_podcastExportScript()">' +
    _pcEsc(_pcT('paper.podcastExportScript', 'Export script (md)')) + '</button>';
  h += '<button class="paper-podcast-btn paper-podcast-btn-ghost" onclick="_podcastGenerate(true)">' +
    _pcEsc(_pcT('paper.podcastRegenerate', 'Regenerate')) + '</button>';
  h += '</div>';

  // Transcript: click a segment to seek (audio mode); prefix sums of
  // est_seconds give the seek offsets.
  var starts = [], acc = 0;
  segs.forEach(function(sg) { starts.push(acc); acc += (sg.est_seconds || 0); });
  h += '<div class="paper-podcast-transcript" id="podcastTranscript">';
  segs.forEach(function(sg, i) {
    h += '<div class="paper-podcast-seg" data-seg="' + i + '"' +
      (!scriptOnly ? ' onclick="_podcastSeekSegment(' + i + ')"' : '') + '>' +
      '<span class="paper-podcast-seg-time">' + _pcFmtSec(starts[i]) + '</span>' +
      '<p>' + _pcEsc(sg.text) + '</p></div>';
  });
  h += '</div></div>';
  host.innerHTML = h;

  if (!scriptOnly && audioUrl) {
    var audio = document.getElementById('podcastAudio');
    if (audio) {
      audio.addEventListener('timeupdate', function() {
        _pcHighlightSegment(audio.currentTime, starts);
        _pcSleepTick(audio);
      });
    }
  }
}

function _pcFmtSec(x) {
  x = Math.max(0, Math.floor(x || 0));
  return Math.floor(x / 60) + ':' + ('0' + (x % 60)).slice(-2);
}

function _pcHighlightSegment(now, starts) {
  var cur = 0;
  for (var i = 0; i < starts.length; i++) { if (now >= starts[i]) cur = i; }
  var list = document.querySelectorAll('#podcastTranscript .paper-podcast-seg');
  list.forEach(function(el) {
    el.classList.toggle('active', parseInt(el.dataset.seg, 10) === cur);
  });
}

/** Click-to-seek: jump the player to a transcript segment's start offset. */
function _podcastSeekSegment(i) {
  var audio = document.getElementById('podcastAudio');
  var d = _podcast.data || {};
  var segs = (d.script && d.script.segments) || [];
  if (!audio || !segs.length) return;
  var start = 0;
  for (var k = 0; k < i && k < segs.length; k++) start += (segs[k].est_seconds || 0);
  try { audio.currentTime = start; } catch (e) { console.warn('[Paper:Podcast] seek failed:', e); }
}

// ── Sleep timer (owner P1: "listen before sleep" is a first-class case) ──

function _podcastSleepTimerChange() {
  var sel = document.getElementById('podcastSleepSel');
  var mins = sel ? parseInt(sel.value, 10) || 0 : 0;
  if (_podcast.sleepTimerId) { clearTimeout(_podcast.sleepTimerId); _podcast.sleepTimerId = 0; }
  _podcast.sleepDeadline = 0;
  var note = document.getElementById('podcastSleepNote');
  if (note) note.textContent = '';
  if (mins > 0) {
    _podcast.sleepDeadline = Date.now() + mins * 60000;
    _podcast.sleepTimerId = setTimeout(function() {
      var audio = document.getElementById('podcastAudio');
      if (audio) { try { audio.pause(); } catch (e) { console.warn('[Paper:Podcast] sleep pause failed:', e); } }
      var n = document.getElementById('podcastSleepNote');
      if (n) n.textContent = '⏸';
      var s2 = document.getElementById('podcastSleepSel');
      if (s2) s2.value = '0';
      _podcast.sleepTimerId = 0;
      _podcast.sleepDeadline = 0;
    }, mins * 60000);
  }
}

/** Update the countdown note on playback ticks (cheap — timeupdate only). */
function _pcSleepTick(audio) {
  if (!_podcast.sleepDeadline) return;
  var note = document.getElementById('podcastSleepNote');
  if (!note) return;
  var left = Math.max(0, _podcast.sleepDeadline - Date.now());
  note.textContent = ' · ' + _pcFmtSec(left / 1000);
}

// ── Script export (client-side markdown) ──

function _podcastExportScript() {
  var d = _podcast.data || {};
  var script = d.script || {};
  var segs = script.segments || [];
  if (!segs.length) return;
  var lines = ['# ' + (script.title || 'Paper Podcast'), ''];
  segs.forEach(function(sg) {
    lines.push('## ' + (sg.section || ''));
    lines.push('');
    lines.push(sg.text || '');
    lines.push('');
  });
  var blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'paper-podcast-script-' + (_podcast.mode || 'short') + '-' +
    (_podcast.paperHash || '').slice(0, 8) + '.md';
  document.body.appendChild(a);
  a.click();
  setTimeout(function() {
    URL.revokeObjectURL(a.href);
    if (a.parentNode) a.parentNode.removeChild(a);
  }, 0);
}
