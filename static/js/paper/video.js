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
  status: 'idle',          // idle|loading|generating|done|report_required|lookup_failed|error
  errorText: '',
  progress: { done: 0, total: 0, phase: '' },
  result: null,            // poll done → {final_path, duration, scenes, narrated}
  scenes: [],              // GET /scenes payload
  regenSceneId: '',
  regenTaskId: '',
  ttsAvailable: true,
  defaultVoice: '',
};
// Poll cadence — a var (not const) so the JSDOM harness can shrink it.
var _PVIDEO_POLL_MS = 1500;

function _pvT(key, fallback) {
  return (typeof t === 'function') ? t(key) : (fallback || key);
}

function _pvEl() { return document.getElementById('paperVideoContent'); }

function _pvEsc(s) {
  return (typeof escapeHtml === 'function') ? escapeHtml(s == null ? '' : s)
    : String(s == null ? '' : s);
}

function _pvStopPoll() {
  if (_pvideo.pollTimer) { clearTimeout(_pvideo.pollTimer); _pvideo.pollTimer = null; }
}

/** Entry point — called by _switchPaperTab('video'). */
async function _initVideoTab(force) {
  var host = _pvEl();
  if (!host) return;
  _pvStopPoll();
  _pvideo.paperHash = (typeof _paperHash !== 'undefined') ? (_paperHash || '') : '';
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
    if (st && st.ok) {
      _pvideo.ttsAvailable = !!st.tts_available;
    }
    var look = await Api.paper.videoLookup({ paper_hash: _pvideo.paperHash });
    if (look && look.ok && look.found) {
      _pvideo._doneTaskId = look.task_id;
      if (look.running) {
        _pvideo.taskId = look.task_id;
        _pvideo.cursor = 0;
        _pvideo.status = 'generating';
        _pvRender();
        _pvSchedulePoll();
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

async function _pvPollOnce() {
  var tid = _pvideo.regenTaskId || _pvideo.taskId;
  if (!tid) return;
  try {
    var resp = await Api.motion.poll(tid, _pvideo.cursor);
    if (!resp || !resp.ok) { _pvSchedulePoll(); return; }
    _pvideo.cursor = resp.next_cursor != null ? resp.next_cursor : _pvideo.cursor;
    (resp.events || []).forEach(function(ev) {
      if (ev.type === 'phase') _pvideo.progress.phase = ev.phase || '';
      if (ev.type === 'scene_done') {
        _pvideo.progress = { done: ev.done || 0, total: ev.total || 0,
                             phase: _pvideo.progress.phase };
      }
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
        _pvRender();
        _pvLoadScenes();
      } else if (resp.status === 'aborted') {
        _pvideo.status = 'idle';
        _pvideo.taskId = '';
        _pvRender();
      } else {
        _pvideo.status = 'error';
        _pvideo.errorText = (resp.error && resp.error.detail) ||
          _pvT('paper.videoFailed', 'Video generation failed');
        _pvideo.taskId = '';
        _pvRender();
      }
      return;
    }
    _pvRenderProgress();
    _pvSchedulePoll();
  } catch (e) {
    console.warn('[Paper:Video] poll failed:', e);
    _pvSchedulePoll();
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
  _pvRender();
  try {
    var resp = await Api.paper.videoStart({
      paper_hash: _pvideo.paperHash,
      lang: _pvideo.lang, voice: _pvideo.voice,
      narration: _pvideo.narration, burn_in: _pvideo.burnIn,
      quality: _pvideo.quality,
    });
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
  _pvStopPoll();
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
    concat: 'paper.videoPhaseConcat', mux: 'paper.videoPhaseMux',
    burn_in: 'paper.videoPhaseBurnIn', regen: 'paper.videoPhaseRegen',
  };
  var fallbacks = {
    parse: 'Parsing subtitles', storyboard: 'Storyboarding',
    narrate: 'Voicing scenes', compose: 'Composing scenes',
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
  el.textContent = (p.total > 0)
    ? label + ' ' + p.done + '/' + p.total
    : (label || _pvT('paper.videoStarting', 'Starting…'));
}

function _pvDegradeBanner() {
  return '<div class="paper-podcast-banner">' +
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
    '<span>' + _pvEsc(_pvT('paper.videoNoTts',
      'No TTS voice slot is configured — this run generates a silent video.')) +
    '</span></div>';
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

  if (s.status === 'idle' || s.status === 'error') {
    h += '<div class="paper-podcast-card">';
    h += '<div class="paper-podcast-hint">' + _pvEsc(_pvT('paper.videoHint',
      'A short narrated motion-graphic video of this paper — beats, charts and kinetic type.')) + '</div>';
    if (!s.ttsAvailable) h += _pvDegradeBanner();
    h += '<div class="paper-podcast-form">';
    h += '<select id="videoLangSel" class="paper-podcast-sel">' +
      '<option value="zh"' + (s.lang === 'zh' ? ' selected' : '') + '>中文</option>' +
      '<option value="en"' + (s.lang === 'en' ? ' selected' : '') + '>English</option></select>';
    h += '<select id="videoQualSel" class="paper-podcast-sel">' +
      '<option value="draft"' + (s.quality === 'draft' ? ' selected' : '') + '>' +
      _pvEsc(_pvT('paper.videoQualityDraft', 'Draft (fast)')) + '</option>' +
      '<option value="standard"' + (s.quality === 'standard' ? ' selected' : '') + '>' +
      _pvEsc(_pvT('paper.videoQualityStandard', 'Standard')) + '</option>' +
      '<option value="high"' + (s.quality === 'high' ? ' selected' : '') + '>' +
      _pvEsc(_pvT('paper.videoQualityHigh', 'High')) + '</option></select>';
    h += '<input id="videoVoiceInp" class="paper-podcast-voice" type="text" value="' +
      _pvEsc(s.voice) + '" placeholder="' + _pvEsc(s.defaultVoice ||
        _pvT('paper.podcastVoice', 'voice (optional)')) + '" />';
    h += '<label class="paper-video-chk"><input id="videoNarrChk" type="checkbox"' +
      (s.narration ? ' checked' : '') + ' />' +
      _pvEsc(_pvT('paper.videoNarration', 'Narration')) + '</label>';
    h += '<label class="paper-video-chk"><input id="videoBurnChk" type="checkbox"' +
      (s.burnIn ? ' checked' : '') + ' />' +
      _pvEsc(_pvT('paper.videoBurnIn', 'Burn-in subtitles')) + '</label>';
    h += '<button class="paper-podcast-btn" onclick="_videoGenerate()">' +
      _pvEsc(_pvT('paper.videoGenerate', 'Generate video')) + '</button>';
    h += '</div>';
    if (s.status === 'error' && s.errorText) {
      h += '<div class="paper-podcast-error">' + _pvEsc(s.errorText) + '</div>';
    }
    h += '</div>';
    host.innerHTML = h;
    return;
  }

  if (s.status === 'generating') {
    h += '<div class="paper-podcast-card">';
    h += '<div class="paper-podcast-progress">';
    h += '<span class="paper-podcast-spinner"></span>';
    h += '<span id="videoProgressLine">' +
      _pvEsc(_pvT('paper.videoStarting', 'Starting…')) + '</span>';
    h += '<button class="paper-podcast-btn paper-podcast-btn-ghost" onclick="_videoAbort()">' +
      _pvEsc(_pvT('paper.podcastAbort', 'Abort')) + '</button>';
    h += '</div></div>';
    host.innerHTML = h;
    _pvRenderProgress();
    return;
  }

  // done
  var r = s.result || {};
  var tid = s._doneTaskId || '';
  var fileUrl = tid ? Api.motion.fileUrl(tid) : '';
  h += '<div class="paper-podcast-card">';
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
      _pvEsc(_pvT('paper.videoDownload', 'Download video')) + '</a>';
    h += '<a class="paper-podcast-btn paper-podcast-btn-ghost" href="' +
      _pvEsc(Api.motion.fileUrl(tid, 'srt')) + '" download="paper-video-' +
      (s.paperHash || '').slice(0, 8) + '.srt">' +
      _pvEsc(_pvT('paper.videoDownloadSrt', 'Download SRT')) + '</a>';
    h += '<button class="paper-podcast-btn paper-podcast-btn-ghost" onclick="_videoGenerate(true)">' +
      _pvEsc(_pvT('paper.podcastRegenerate', 'Regenerate')) + '</button>';
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
  s.scenes.forEach(function(sc) {
    var sid = sc.scene_id;
    var regening = s.regenSceneId === sid;
    var src = Api.motion.sceneFileUrl(tid, sid) +
      (cacheBust ? '?v=' + cacheBust : '');
    h += '<div class="paper-video-cell' + (regening ? ' is-regening' : '') + '">';
    if (sc.has_video) {
      h += '<video class="paper-video-thumb" preload="metadata" muted ' +
        'src="' + _pvEsc(src) + '"' +
        ' onclick="this.paused?this.play():this.pause()"></video>';
    } else {
      h += '<div class="paper-video-thumb paper-video-thumb-empty">…</div>';
    }
    h += '<div class="paper-video-cell-text" title="' + _pvEsc(sc.text || '') + '">' +
      _pvEsc((sc.text || '').slice(0, 42)) + '</div>';
    h += '<button class="paper-video-regen" data-scene="' + _pvEsc(sid) + '"' +
      (regening ? ' disabled' : '') +
      ' onclick="_videoRegenScene(\'' + _pvEsc(sid) + '\')">' +
      (regening ? _pvEsc(_pvT('paper.videoRegening', 'Re-rendering…'))
                : _pvEsc(_pvT('paper.videoRegen', 'Re-render'))) + '</button>';
    h += '</div>';
  });
  h += '</div>';
  grid.innerHTML = h;
}
