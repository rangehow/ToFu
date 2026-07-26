#!/usr/bin/env python3
"""jsdom guards for the P-UX frontend contract (P-UX1~4).

Drives the REAL shipped ``static/js/paper/podcast.js`` and
``static/js/paper/video.js`` under jsdom with stubbed Api seams and probes
the progress-perception / anti-stuck behaviors
(docs/PAPER_MEDIA_UX_DESIGN.md §3.4):

  * P-UX1: 5 consecutive poll failures → honest `lost` terminal state
    (recheck + regenerate buttons wired); server-reaped worker_lost maps
    to the same state. NEUTER: amputating the fail-limit branch restores
    the old infinite-spinner behavior and flips the probe — the backstop
    is load-bearing.
  * P-UX2: generating renders the phase stepper + elapsed/last-activity
    line; phase_started advances the active step; script sub-step labels
    show; >30s silence tints the line stale.
  * P-UX3: video scene_done events incrementally fill the scene grid
    (pending cells light up); ETA appears for render/narrate phases.
  * P-UX4: lookup `interrupted` → honest interrupted card.

Static guards (no node): i18n zh+en keys, CSS classes, JS syntax.
Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
PODCAST_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'podcast.js')
VIDEO_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'video.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_PODCAST_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="paperPodcastContent"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
const T_MAP = {
  'paper.podcastLost': 'LOST_TEXT',
  'paper.podcastInterrupted': 'INTERRUPTED_TEXT',
  'paper.podcastRecheck': 'RECHECK_BTN',
  'paper.podcastRegenerate': 'REGEN_BTN',
  'paper.podcastPhaseSource': 'STEP_SOURCE',
  'paper.podcastPhaseScript': 'STEP_SCRIPT',
  'paper.podcastPhaseAudio': 'STEP_AUDIO',
  'paper.podcastStepValidate': 'VALIDATING',
  'paper.podcastScriptPhase': 'WRITING_SCRIPT',
  'paper.podcastAudioPhase': 'SYNTH_AUDIO',
  'paper.mediaEtaPrefix': '≈',
  'paper.mediaElapsed': 'elapsed',
  'paper.mediaLastActive': 'last-active',
  'paper.mediaStillRunning': 'STILL_RUNNING',
};
win.t = global.t = (k) => T_MAP[k] || k;
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
win._paperHash = 'hash123abc';
global._paperHash = 'hash123abc';

const apiState = {
  statusResp: { ok: true, tts_available: true, default_voice: 'alloy' },
  lookupResp: { ok: true, found: false, report_available: true },
  startResp: { ok: true, task_id: 'podcast_x1' },
  pollQueue: [],
  pollNull: false,
};
global.Api = win.Api = { paper: {
  podcastStatus: async () => apiState.statusResp,
  podcastLookup: async () => apiState.lookupResp,
  podcastStart: async () => apiState.startResp,
  podcastPoll: async () => {
    if (apiState.pollNull) return null;
    return apiState.pollQueue.length
      ? apiState.pollQueue.shift()
      : { ok: true, done: false, cursor: 99, events: [] };  // no progress key → client keeps the last counts
  },
  podcastAbort: async () => ({}),
  podcastScript: async () => ({}),
}};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/podcast.js (real)
_PODCAST_POLL_MS = 1;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
function host() { return document.getElementById('paperPodcastContent'); }
async function settle(n) { for (let i = 0; i < n; i++) await new Promise(r => setTimeout(r, 3)); }

(async () => {
  // ── Case A: 5 consecutive poll failures → lost (P-UX1 backstop) ──
  apiState.lookupResp = { ok: true, found: false, report_available: true };
  await _initPodcastTab();
  apiState.pollNull = true;
  await _podcastGenerate();
  await settle(25);
  check('lost_after_5_fails', _podcast.status === 'lost');
  check('lost_render_text', host().innerHTML.includes('LOST_TEXT'));
  const recheckBtn = Array.from(host().querySelectorAll('button'))
    .find(b => (b.getAttribute('onclick') || '').includes('_initPodcastTab'));
  check('lost_recheck_wired', !!recheckBtn);
  const regenBtn = Array.from(host().querySelectorAll('button'))
    .find(b => (b.getAttribute('onclick') || '').includes('_podcastGenerate(true)'));
  check('lost_regen_wired', !!regenBtn);
  // spinner has a lifetime: no poll timer survives the lost state
  check('lost_stops_polling', _podcast.pollTimer === null && _podcast.taskId === '');

  // ── Case B: server-reaped worker_lost → same lost state ──
  apiState.pollNull = false;
  apiState.pollQueue = [
    { ok: true, done: true, status: 'error', cursor: 1, events: [],
      error: { kind: 'worker_lost', detail: 'no progress events' } },
  ];
  await _podcastGenerate(true);
  await settle(8);
  check('worker_lost_maps_to_lost', _podcast.status === 'lost');

  // ── Case C: stepper + phase events + sub-step + ETA + stale tint ──
  apiState.pollQueue = [
    { ok: true, done: false, cursor: 1, progress: { done: 0, total: 0 },
      events: [{ type: 'phase_started', phase: 'script', phase_index: 2,
                 phase_total: 3, phases: ['source', 'script', 'audio'] }] },
    { ok: true, done: false, cursor: 2, progress: { done: 0, total: 0 },
      events: [{ type: 'progress', phase: 'script', unit: 'pass', step: 'validate' }] },
    { ok: true, done: false, cursor: 3, progress: { done: 1, total: 4 },
      events: [{ type: 'phase_started', phase: 'audio', phase_index: 3,
                 phase_total: 3, phases: ['source', 'script', 'audio'] },
               { type: 'segment_done', done: 1, total: 4 }] },
    { ok: true, done: false, cursor: 4, progress: { done: 2, total: 4 },
      events: [{ type: 'segment_done', done: 2, total: 4 }] },
  ];
  await _podcastGenerate(true);
  await settle(14);
  const html = host().innerHTML;
  check('stepper_rendered', !!host().querySelector('.paper-stepper'));
  check('stepper_labels', html.includes('STEP_SOURCE') && html.includes('STEP_SCRIPT')
    && html.includes('STEP_AUDIO'));
  check('stepper_third_active', !!host().querySelectorAll('.paper-step.is-active')[0]
    && host().querySelectorAll('.paper-step.is-active')[0].textContent.includes('STEP_AUDIO'));
  check('stepper_first_two_done',
    host().querySelectorAll('.paper-step.is-done').length === 2);
  check('progress_counts', html.includes('2/4'));
  // ETA: fake an old first tick so the wall-clock rate is visible
  _podcast._segFirstTick = Date.now() - 60000;
  _pcConsumeEvent({ type: 'segment_done', done: 3, total: 4 });
  _pcRenderProgress();
  check('eta_shown', host().innerHTML.includes('≈'));
  // sub-step label rendered while in script phase
  _podcast.progress = { done: 0, total: 0 };
  _podcast.scriptStep = 'validate';
  _pcRenderProgress();
  check('script_substep_shown', host().innerHTML.includes('VALIDATING'));
  // activity line + stale tint after 30s of silence
  check('activity_line_present', !!document.getElementById('podcastActivityLine'));
  _podcast.lastEventAt = Date.now() - 31000;
  _pcRenderActivity();
  const act = document.getElementById('podcastActivityLine');
  check('activity_stale_tint', act.classList.contains('is-stale')
    && act.textContent.includes('STILL_RUNNING'));
  _pcStopPoll();

  // ── Case D: interrupted lookup → honest interrupted card (P-UX4) ──
  apiState.lookupResp = { ok: true, found: true, interrupted: true,
                          report_available: true };
  await _initPodcastTab(true);
  check('interrupted_state', _podcast.status === 'interrupted');
  check('interrupted_render', host().innerHTML.includes('INTERRUPTED_TEXT')
    && host().innerHTML.includes('REGEN_BTN'));

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


_VIDEO_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="paperVideoContent"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
const T_MAP = {
  'paper.podcastLost': 'LOST_TEXT',
  'paper.podcastInterrupted': 'INTERRUPTED_TEXT',
  'paper.podcastRecheck': 'RECHECK_BTN',
  'paper.podcastRegenerate': 'REGEN_BTN',
  'paper.videoPhaseStoryboard': 'PH_STORYBOARD',
  'paper.videoPhaseNarrate': 'PH_NARRATE',
  'paper.videoPhaseCompose': 'PH_COMPOSE',
  'paper.videoPhaseRender': 'PH_RENDER',
  'paper.videoPhaseConcat': 'PH_CONCAT',
  'paper.videoPhaseMux': 'PH_MUX',
  'paper.videoStarting': 'STARTING',
  'paper.videoScenesTitle': 'SCENES_TITLE',
  'paper.mediaEtaPrefix': '≈',
  'paper.mediaElapsed': 'elapsed',
  'paper.mediaLastActive': 'last-active',
  'paper.mediaStillRunning': 'STILL_RUNNING',
};
win.t = global.t = (k) => T_MAP[k] || k;
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
win._paperHash = 'hash123abc';
global._paperHash = 'hash123abc';

const PHASES = ['storyboard', 'narrate', 'compose', 'render', 'concat', 'mux'];
const apiState = {
  lookupResp: { ok: true, found: false, report_available: true },
  startResp: { ok: true, task_id: 'motion_x1' },
  pollQueue: [],
  pollNull: false,
  scenesCalls: 0,
  scenesResp: { ok: true, task_id: 'motion_x1', status: 'running', scenes: [
    { scene_id: 'scene-001', start: 0, end: 4, text: '第一句',
      has_composition: true, has_video: true, has_narration: true },
    { scene_id: 'scene-002', start: 4, end: 8, text: '第二句',
      has_composition: true, has_video: false, has_narration: false },
  ]},
};
global.Api = win.Api = {
  motion: {
    status: async () => ({ ok: true, tts_available: true }),
    poll: async () => {
      if (apiState.pollNull) return null;
      return apiState.pollQueue.length
        ? apiState.pollQueue.shift()
        : { ok: true, done: false, next_cursor: 99, events: [] };
    },
    abort: async () => ({}),
    scenes: async () => { apiState.scenesCalls++; return apiState.scenesResp; },
    regenScene: async () => ({ ok: true, task_id: 'regen_1' }),
    fileUrl: (tid) => '/file/' + tid,
    sceneFileUrl: (tid, sid) => '/file/' + tid + '/' + sid,
  },
  paper: {
    videoLookup: async () => apiState.lookupResp,
    videoStart: async () => apiState.startResp,
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/video.js (real)
_PVIDEO_POLL_MS = 1;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
function host() { return document.getElementById('paperVideoContent'); }
async function settle(n) { for (let i = 0; i < n; i++) await new Promise(r => setTimeout(r, 3)); }

(async () => {
  // ── Case A: generating → stepper + grid fills on scene_done (P-UX2/3) ──
  apiState.lookupResp = { ok: true, found: false, report_available: true };
  await _initVideoTab();
  apiState.pollQueue = [
    { ok: true, done: false, next_cursor: 1,
      events: [{ type: 'phase_started', phase: 'storyboard', phase_index: 1,
                 phase_total: 6, phases: PHASES }] },
    { ok: true, done: false, next_cursor: 2,
      events: [{ type: 'phase_started', phase: 'render', phase_index: 4,
                 phase_total: 6, phases: PHASES }] },
    { ok: true, done: false, next_cursor: 3,
      events: [{ type: 'scene_done', scene_id: 'scene-001', ok: true,
                 done: 1, total: 2 }] },
  ];
  await _videoGenerate();
  await settle(12);
  const html = host().innerHTML;
  check('stepper_rendered', !!host().querySelector('.paper-stepper'));
  check('stepper_render_active', (() => {
    const act = host().querySelector('.paper-step.is-active');
    return !!act && act.textContent.includes('PH_RENDER');
  })());
  check('stepper_three_done',
    host().querySelectorAll('.paper-step.is-done').length === 3);
  check('progress_counts', html.includes('1/2'));
  check('grid_present_in_generating', !!document.getElementById('paperVideoGrid'));
  check('scenes_refetched_on_scene_done', apiState.scenesCalls >= 1);
  const cells = host().querySelectorAll('.paper-video-cell');
  check('grid_cells_rendered', cells.length === 2);
  check('grid_pending_cell_marked',
    host().querySelectorAll('.paper-video-cell.is-pending').length === 1);
  check('grid_lit_cell_has_video',
    !!host().querySelector('.paper-video-cell video.paper-video-thumb'));
  check('no_regen_buttons_while_generating',
    host().querySelectorAll('.paper-video-regen').length === 0);
  // ETA in render phase (fake an old rate tick so the rate is visible)
  _pvideo._rateFirstTick = Date.now() - 60000;
  _pvideo._rateFirstDone = 1;
  _pvConsumeEvent({ type: 'scene_done', scene_id: 'scene-002', ok: true,
                    done: 2, total: 3 });
  _pvRenderProgress();
  check('eta_shown_in_render', host().innerHTML.includes('≈'));
  // activity line + stale tint
  check('activity_line_present', !!document.getElementById('videoActivityLine'));
  _pvideo.lastEventAt = Date.now() - 31000;
  _pvRenderActivity();
  const act2 = document.getElementById('videoActivityLine');
  check('activity_stale_tint', act2.classList.contains('is-stale')
    && act2.textContent.includes('STILL_RUNNING'));
  _pvStopPoll();

  // ── Case B: 5 consecutive poll failures → lost (P-UX1) ──
  apiState.pollNull = true;
  await _videoGenerate(true);
  await settle(25);
  check('lost_after_5_fails', _pvideo.status === 'lost');
  check('lost_render_text', host().innerHTML.includes('LOST_TEXT'));
  check('lost_stops_polling', _pvideo.pollTimer === null && _pvideo.taskId === '');

  // ── Case C: server-reaped worker_lost → lost ──
  apiState.pollNull = false;
  apiState.pollQueue = [
    { ok: true, done: true, status: 'error', next_cursor: 1, events: [],
      error: { kind: 'worker_lost', detail: 'no progress events' } },
  ];
  await _videoGenerate(true);
  await settle(8);
  check('worker_lost_maps_to_lost', _pvideo.status === 'lost');

  // ── Case D: interrupted lookup → honest card (P-UX4) ──
  apiState.lookupResp = { ok: true, found: true, interrupted: true,
                          task_id: 'motion_old', report_available: true };
  await _initVideoTab(true);
  check('interrupted_state', _pvideo.status === 'interrupted');
  check('interrupted_render', host().innerHTML.includes('INTERRUPTED_TEXT')
    && host().innerHTML.includes('REGEN_BTN'));

  // ── Case E: regenerate passes force (dedup bypass, §2.1) ──
  let startBody = null;
  Api.paper.videoStart = async (b) => { startBody = b; return { ok: true, task_id: 'motion_f' }; };
  await _videoGenerate(true);
  check('regenerate_sends_force', !!startBody && startBody.force === true);

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run_harness(harness_src: str, module_path: str, name: str) -> str:
    harness = os.path.join(HERE, f'_{name}_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(harness_src)
    try:
        proc = subprocess.run(
            ['node', harness, module_path, ROOT],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_podcast_ux_state_machine():
    out = _run_harness(_PODCAST_HARNESS, PODCAST_JS, 'pux_podcast')
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'podcast P-UX failures:\n' + out
    assert out.count('PASS') >= 15, f'expected >=15 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_video_ux_state_machine():
    out = _run_harness(_VIDEO_HARNESS, VIDEO_JS, 'pux_video')
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'video P-UX failures:\n' + out
    assert out.count('PASS') >= 15, f'expected >=15 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_podcast_fail_limit_loadbearing():
    """NEUTER: amputate the 5-strike branch from a COPY of podcast.js →
    polls reschedule forever, `lost_after_5_fails` flips to FAIL — the
    backstop is what stands between a 404 and an infinite spinner."""
    src = open(PODCAST_JS, encoding='utf-8').read()
    marker = 'if (_podcast.pollFails >= _PC_POLL_FAIL_LIMIT) {'
    assert marker in src, 'fail-limit marker not found — test is stale'
    broken = src.replace(marker, 'if (false) {', 1)
    assert broken != src

    tmp = os.path.join(HERE, '_podcast_no_limit.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True,
                             text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        out = _run_harness(_PODCAST_HARNESS, tmp, 'pux_podcast_neuter')
        assert 'FAIL lost_after_5_fails' in out, \
            'amputating the fail limit did NOT flip the probe:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    assert open(PODCAST_JS, encoding='utf-8').read() == src, 'shipped file modified!'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_video_fail_limit_loadbearing():
    """NEUTER: same amputation on video.js — the lost state must vanish."""
    src = open(VIDEO_JS, encoding='utf-8').read()
    marker = 'if (_pvideo.pollFails >= _PV_POLL_FAIL_LIMIT) {'
    assert marker in src, 'fail-limit marker not found — test is stale'
    broken = src.replace(marker, 'if (false) {', 1)
    assert broken != src

    tmp = os.path.join(HERE, '_video_no_limit.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True,
                             text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        out = _run_harness(_VIDEO_HARNESS, tmp, 'pux_video_neuter')
        assert 'FAIL lost_after_5_fails' in out, \
            'amputating the fail limit did NOT flip the probe:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    assert open(VIDEO_JS, encoding='utf-8').read() == src, 'shipped file modified!'


# ═══ Static guards (no node required) ═══

def test_static_i18n_keys():
    src = open(os.path.join(ROOT, 'static', 'js', 'i18n.js'), encoding='utf-8').read()
    keys = ('paper.podcastPhaseSource', 'paper.podcastPhaseScript',
            'paper.podcastPhaseAudio', 'paper.podcastStepDraft',
            'paper.podcastStepValidate', 'paper.podcastStepRevise',
            'paper.podcastStepCritic', 'paper.podcastLost',
            'paper.podcastInterrupted', 'paper.podcastRecheck',
            'paper.mediaElapsed', 'paper.mediaLastActive',
            'paper.mediaStillRunning', 'paper.mediaEtaPrefix',
            'paper.videoPhaseRender')
    for key in keys:
        m = re.search(re.escape(f"'{key}'") +
                      r":\s*\{\s*zh:\s*'((?:[^'\\]|\\.)*)',\s*en:\s*'((?:[^'\\]|\\.)*)'\s*\}",
                      src)
        assert m, f'i18n key {key} missing'
        assert m.group(1).strip() and m.group(2).strip(), \
            f'i18n key {key} has an empty zh or en'


def test_static_css_classes():
    css = open(os.path.join(ROOT, 'static', 'styles.css'), encoding='utf-8').read()
    for cls in ('.paper-stepper', '.paper-step.is-active',
                '.paper-step.is-done', '.paper-media-activity',
                '.paper-media-activity.is-stale', '.paper-video-cell.is-pending'):
        assert cls in css, f'CSS class {cls} missing'


def test_static_js_syntax():
    if not shutil.which('node'):
        pytest.skip('node not installed')
    for path in (PODCAST_JS, VIDEO_JS):
        proc = subprocess.run(['node', '--check', path],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f'{path} syntax: {proc.stderr}'


if __name__ == '__main__':
    import sys
    test_static_i18n_keys()
    test_static_css_classes()
    if _node_deps_available():
        test_static_js_syntax()
        test_podcast_ux_state_machine()
        test_video_ux_state_machine()
        test_NEUTER_podcast_fail_limit_loadbearing()
        test_NEUTER_video_fail_limit_loadbearing()
    else:
        print('SKIP jsdom cases — node + jsdom not available')
    print('ALL PASSED')
    sys.exit(0)
