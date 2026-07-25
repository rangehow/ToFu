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
  status: 'idle',          // idle|generating|done|script_only|report_required|error
  data: null,              // {script, meta, audioUrl, durationSec, scriptOnly}
  errorText: '',
  progress: { done: 0, total: 0 },
  ttsAvailable: true,
  defaultVoice: '',
  sleepTimerId: 0,
  sleepDeadline: 0,
};
// Poll cadence — a var (not const) so the JSDOM harness can shrink it.
var _PODCAST_POLL_MS = 1200;

function _pcT(key, fallback) {
  return (typeof t === 'function') ? t(key) : (fallback || key);
}

function _pcEl() { return document.getElementById('paperPodcastContent'); }

function _pcEsc(s) {
  return (typeof escapeHtml === 'function') ? escapeHtml(s == null ? '' : s)
    : String(s == null ? '' : s);
}

function _pcStopPoll() {
  if (_podcast.pollTimer) { clearTimeout(_podcast.pollTimer); _podcast.pollTimer = null; }
}

/** Entry point — called by _switchPaperTab('podcast'). Renders the tab by
 * looking the paper up server-side: a live task re-attaches, a cached
 * podcast renders instantly, otherwise the generate card shows. */
async function _initPodcastTab(force) {
  var host = _pcEl();
  if (!host) return;
  _pcStopPoll();
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
      _pcRender();
      _pcSchedulePoll();
      return;
    }
    if (look && look.ok && look.found && look.cached) {
      _podcast.data = look;
      _podcast.status = look.scriptOnly ? 'script_only' : 'done';
      _pcRender();
      return;
    }
    _podcast.reportAvailable = !!(look && look.report_available);
    _podcast.status = _podcast.reportAvailable ? 'idle' : 'report_required';
    _pcRender();
  } catch (e) {
    console.warn('[Paper:Podcast] lookup failed:', e);
    _podcast.status = 'idle';
    _pcRender();
  }
}

function _pcSchedulePoll() {
  _pcStopPoll();
  _podcast.pollTimer = setTimeout(_pcPollOnce, _PODCAST_POLL_MS);
}

async function _pcPollOnce() {
  if (!_podcast.taskId) return;
  try {
    var resp = await Api.paper.podcastPoll(_podcast.taskId, _podcast.cursor);
    if (!resp || !resp.ok) { _pcSchedulePoll(); return; }
    _podcast.cursor = resp.cursor || _podcast.cursor;
    (resp.events || []).forEach(function(ev) {
      if (ev.type === 'segment_done') {
        _podcast.progress = { done: ev.done, total: ev.total };
      }
    });
    _podcast.progress = resp.progress || _podcast.progress;
    if (resp.done) {
      if (resp.status === 'done') {
        _podcast.data = resp;
        _podcast.status = resp.scriptOnly ? 'script_only' : 'done';
      } else if (resp.status === 'aborted') {
        _podcast.status = 'idle';
      } else {
        _podcast.status = 'error';
        _podcast.errorText = resp.error || _pcT('paper.podcastFailed', 'Podcast generation failed');
      }
      _podcast.taskId = '';
      _pcRender();
      return;
    }
    _pcRenderProgress();
    _pcSchedulePoll();
  } catch (e) {
    console.warn('[Paper:Podcast] poll failed:', e);
    _pcSchedulePoll();
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
  _pcStopPoll();
  _podcast.taskId = '';
  _podcast.status = 'idle';
  _pcRender();
}

// ── Rendering ──

function _pcRenderProgress() {
  var el = document.getElementById('podcastProgressLine');
  if (!el) return;
  var p = _podcast.progress;
  el.textContent = (p.total > 0)
    ? _pcT('paper.podcastAudioPhase', 'Synthesizing audio') + ' ' + p.done + '/' + p.total
    : _pcT('paper.podcastScriptPhase', 'Writing the spoken script…');
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
      '<div class="paper-report-empty paper-podcast-empty">' +
      '<p>' + _pcEsc(_pcT('paper.podcastNeedReport',
        'The podcast is adapted from the analysis report — generate the report first.')) + '</p>' +
      '<button class="paper-podcast-btn" onclick="_switchPaperTab(\'report\')">' +
      _pcEsc(_pcT('paper.podcastGoReport', 'Go generate the report')) + '</button>' +
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
    h += '<div class="paper-podcast-progress">';
    h += '<span class="paper-podcast-spinner"></span>';
    h += '<span id="podcastProgressLine">' +
      _pcEsc(_pcT('paper.podcastScriptPhase', 'Writing the spoken script…')) + '</span>';
    h += '<button class="paper-podcast-btn paper-podcast-btn-ghost" onclick="_podcastAbort()">' +
      _pcEsc(_pcT('paper.podcastAbort', 'Abort')) + '</button>';
    h += '</div></div>';
    host.innerHTML = h;
    _pcRenderProgress();
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
