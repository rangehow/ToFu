"""tests/test_background_activity_chip.py — background-activity chip wiring.

Guards the convStatusStrip "still working" chip (background-activity.js +
GET /api/v1/activity/background): the chip must render inside
``#convStatusStrip`` before ``#turnNav``, stay hidden in the empty steady
state, surface live swarm sub-agents + detached background commands once the
snapshot reports them, deep-link a swarm row to its conversation, and hide
again when the snapshot clears. User-derived strings (agent objectives,
command lines) must render as TEXT, never markup.

The harness executes the authored section directly, independent of generated
asset freshness. A neuter on the deep-link call proves the assertion is
load-bearing.

DB-free; skips when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SECTIONS = os.path.join(ROOT, 'frontend', 'src', 'runtime', 'sections')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


def _runtime_section(name: str) -> str:
    with open(os.path.join(SECTIONS, name), encoding='utf-8') as f:
        return f.read()


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div class="conv-status-strip" id="convStatusStrip">' +
  '<div class="turn-nav" id="turnNav"></div></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
Object.defineProperty(win.document, 'hidden', { value: false, configurable: true });
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
global.runtimeScope = win.runtimeScope = {};
global.activeConvId = 'conv-1';

// ── Controllable Api stub ──
let SNAPSHOT = { swarms: [], backgroundCommands: [] };
global.Api = win.Api = {
  activity: { background: () => Promise.resolve(SNAPSHOT) },
};
let loadedConv = null;
global.getConvById = win.getConvById =
  (id) => (id === 'conv-1' ? { id: 'conv-1', title: 'Refactor session' } : null);
global.loadConversation = win.loadConversation = (id) => { loadedConv = id; };

eval(fs.readFileSync(process.argv[2], 'utf8'));  // background-activity.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const ACTIVE = {
  swarms: [{
    swarm_key: 'conv-1', conv_id: 'conv-1',
    running: 1, pending: 1, completed: 2, agent_count: 2,
    agents: [
      { id: 'a1', role: 'coder', objective: 'Rewrite the <b>parser</b>',
        status: 'running', round: 3, error: '' },
      { id: 'a2', role: 'reviewer', objective: 'Audit the diff',
        status: 'pending', round: 0, error: '' },
    ],
    created_at: Date.now() / 1000 - 65,
  }],
  backgroundCommands: [{
    command_id: 'bg_abc', conv_id: 'conv-1', command: 'npm run build --workspace=x',
    status: 'running', started_at: Date.now() / 1000 - 10,
    elapsed_s: 10, delivery_status: 'not_applicable',
  }],
};

(async () => {
  const strip = document.getElementById('convStatusStrip');
  // Chip mounts lazily on the first applied snapshot — force a tick while
  // still empty: it must stay hidden AND stay inside the strip.
  runtimeScope.updateBackgroundActivity();
  await sleep(60);
  const chip = document.getElementById('bgActivityChip');
  check('chip_in_strip', !!chip && chip.parentElement === strip);
  check('chip_before_turnnav',
        !!chip && chip.nextElementSibling === document.getElementById('turnNav'));
  check('chip_hidden_when_idle', !!chip && chip.style.display === 'none');

  // Active snapshot → chip visible with the combined label.
  SNAPSHOT = ACTIVE;
  runtimeScope.updateBackgroundActivity();
  await sleep(60);
  check('chip_visible_when_active', chip.style.display !== 'none');
  const label = chip.querySelector('.bg-act-label').textContent;
  check('chip_label_agents', label.includes('2 sub-agents running'));
  check('chip_label_commands', label.includes('1 background command running'));

  // Popover: sections + rows render, objectives/commands as TEXT.
  chip.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  const pop = document.getElementById('bgActivityPopover');
  check('popover_opens', !!pop);
  check('popover_sections',
        !!pop && pop.textContent.includes('Sub-agents') &&
        pop.textContent.includes('Background commands'));
  check('popover_conv_title',
        !!pop && pop.textContent.includes('Refactor session'));
  check('agent_role_rendered',
        !!pop && !!pop.querySelector('.bg-act-agent-role') &&
        pop.querySelector('.bg-act-agent-role').textContent === 'coder');
  check('agent_round_shown',
        !!pop && pop.querySelector('.bg-act-agent-status').textContent
                     .includes('round 3'));
  check('command_rendered',
        !!pop && pop.textContent.includes('npm run build --workspace=x'));
  check('objective_is_text_not_markup',
        !!pop && pop.querySelectorAll('.bg-act-agent-objective b').length === 0 &&
        pop.querySelector('.bg-act-agent-objective').textContent
            .includes('<b>parser</b>'));

  // Deep-link: swarm row click loads its conversation and closes the popover.
  pop.querySelector('.bg-act-swarm').dispatchEvent(
    new win.MouseEvent('click', { bubbles: true }));
  check('swarm_deeplinks', loadedConv === 'conv-1');
  check('popover_closed_after_deeplink',
        !document.getElementById('bgActivityPopover'));

  // Navigation reprojects synchronously, including an outstanding old poll.
  chip.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  let resolveOld;
  Api.activity.background = () => new Promise(resolve => { resolveOld = resolve; });
  runtimeScope.updateBackgroundActivity();
  await sleep(60);
  global.activeConvId = 'conv-2';
  runtimeScope.updateBackgroundActivity();
  check('other_conversation_hidden_immediately', chip.style.display === 'none');
  check('old_popover_closed_on_switch', !document.getElementById('bgActivityPopover'));
  resolveOld(ACTIVE);
  await sleep(60);
  check('late_response_cannot_leak', chip.style.display === 'none');
  Api.activity.background = () => Promise.resolve(SNAPSHOT);
  global.activeConvId = 'conv-1';
  runtimeScope.updateBackgroundActivity();
  check('return_restores_current_activity', chip.style.display !== 'none');
  global.activeConvId = null;
  runtimeScope.updateBackgroundActivity();
  check('new_chat_hidden_immediately', chip.style.display === 'none');
  global.activeConvId = 'conv-1';

  // Snapshot clears → chip hides again.
  SNAPSHOT = { swarms: [], backgroundCommands: [] };
  runtimeScope.updateBackgroundActivity();
  await sleep(60);
  check('chip_hides_when_cleared', chip.style.display === 'none');

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run(section_source: str, harness_source: str = _HARNESS) -> subprocess.CompletedProcess:
    fd, harness = tempfile.mkstemp(
        prefix='_bg_activity_harness_', suffix='.js', dir=HERE)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(harness_source)
    src_fd, src_path = tempfile.mkstemp(
        prefix='_bg_activity_section_', suffix='.js', dir=HERE)
    with os.fdopen(src_fd, 'w', encoding='utf-8') as f:
        f.write(section_source)
    try:
        return subprocess.run(
            ['node', harness, src_path, ROOT],
            capture_output=True, text=True, timeout=60)
    finally:
        for p in (harness, src_path):
            try:
                os.remove(p)
            except OSError:
                pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_background_activity_chip_renders_and_wires():
    proc = _run(_runtime_section('background-activity.js'))
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'background-activity chip failures:\n' + out
    assert out.count('PASS') >= 14, f'expected >=14 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_neuter_deeplink_is_load_bearing():
    """Drop the loadConversation deep-link → 'swarm_deeplinks' must FAIL,
    proving the wiring assertion has teeth."""
    src = _runtime_section('background-activity.js')
    marker = 'loadConversation(swarm.conv_id);'
    assert marker in src, 'NC marker missing — test stale vs background-activity.js'
    patched = src.replace(marker, '/* neutered */ void swarm;', 1)
    proc = _run(patched)
    assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{proc.stdout}'
    assert 'FAIL swarm_deeplinks' in proc.stdout, \
        'NC: neutering the deep-link did NOT break swarm_deeplinks:\n' + proc.stdout


_LIFECYCLE_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { JSDOM } = require(path.join(process.argv[3], 'node_modules', 'jsdom'));
const dom = new JSDOM('<body><div id="convStatusStrip"><div id="turnNav"></div></div>');
global.window = dom.window;
global.document = window.document;
let hidden = false;
Object.defineProperty(document, 'hidden', { get: () => hidden });
Object.defineProperty(document, 'readyState', { get: () => 'loading' });
global.runtimeScope = {};
global.activeConvId = 'one';
let dispose;
global.retainedCompositionLifecycle = { add: fn => { dispose = fn; } };
let now = 100000;
Date.now = () => now;
let serial = 0;
const timers = new Map();
global.setTimeout = (fn, ms) => { timers.set(++serial, { fn, ms }); return serial; };
global.clearTimeout = id => timers.delete(id);
const listeners = new Map();
const add = document.addEventListener.bind(document);
const remove = document.removeEventListener.bind(document);
document.addEventListener = (type, fn, opts) => {
  if (!listeners.has(type)) listeners.set(type, new Set());
  listeners.get(type).add(fn); add(type, fn, opts);
};
document.removeEventListener = (type, fn, opts) => {
  listeners.get(type)?.delete(fn); remove(type, fn, opts);
};
let calls = 0;
let resolveFlight;
let fail = false;
let snapshot = {
  swarms: [{ swarm_key: 's', conv_id: 'one', created_at: 90, running: 1, agents: [] }],
  backgroundCommands: [{ command_id: 'c', conv_id: 'one', command: '<script>bad</script>', elapsed_s: 10 }]
};
global.Api = { activity: { background: () => {
  calls++;
  if (fail) throw Error('sync failure');
  return new Promise(resolve => { resolveFlight = resolve; });
} } };
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const tick = async () => {
  assert.equal(timers.size, 1, 'one pending timeout');
  const [id, timer] = timers.entries().next().value;
  timers.delete(id); timer.fn(); await flush();
};
const settle = async () => { resolveFlight(snapshot); await flush(); };
eval(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
  runtimeScope.updateBackgroundActivity();
  document.dispatchEvent(new window.Event('DOMContentLoaded'));
  assert.equal([...timers.values()][0].ms, 0, 'DCL preserves immediate refresh');
  await tick();
  for (let i = 0; i < 100; i++) runtimeScope.updateBackgroundActivity();
  assert.equal(calls, 1, 'refresh bursts share one request');
  assert.equal(timers.size, 0, 'no recurring wakeup while request outstanding');
  await settle();
  assert.equal([...timers.values()][0].ms, 3000);
  const chip = document.getElementById('bgActivityChip');
  chip.click();
  const pop = document.getElementById('bgActivityPopover');
  const row = pop.querySelector('.bg-act-swarm');
  const cmd = pop.querySelector('.bg-act-cmd');
  assert.equal(cmd.querySelectorAll('script').length, 0);
  assert.equal(row.querySelector('.bg-act-elapsed').textContent, '10s');
  now += 3000; snapshot.backgroundCommands[0].elapsed_s = 13;
  await tick(); await settle();
  assert.strictEqual(pop.querySelector('.bg-act-swarm'), row, 'unchanged row identity');
  assert.strictEqual(pop.querySelector('.bg-act-cmd'), cmd, 'command row identity');
  assert.equal(row.querySelector('.bg-act-elapsed').textContent, '13s');
  assert.equal(cmd.querySelector('.bg-act-elapsed').textContent, '13s');
  hidden = true; document.dispatchEvent(new window.Event('visibilitychange'));
  assert.equal(timers.size, 0, 'hidden cancels scheduled wakeup');
  runtimeScope.updateBackgroundActivity();
  assert.equal(timers.size, 0, 'hidden explicit refresh does not poll');
  hidden = false; document.dispatchEvent(new window.Event('visibilitychange'));
  assert.equal([...timers.values()][0].ms, 0, 'visible resumes immediately');
  await tick();
  hidden = true; document.dispatchEvent(new window.Event('visibilitychange'));
  await settle();
  assert.equal(timers.size, 0, 'hidden flight settlement does not rearm');
  hidden = false; document.dispatchEvent(new window.Event('visibilitychange'));
  fail = true; await tick();
  assert.equal([...timers.values()][0].ms, 10000, 'synchronous API failure backs off');
  fail = false; await tick();
  chip.click();
  dispose(); dispose();
  await settle();
  runtimeScope.updateBackgroundActivity();
  document.dispatchEvent(new window.Event('visibilitychange'));
  document.dispatchEvent(new window.Event('DOMContentLoaded'));
  assert.equal(timers.size, 0, 'disposed late response and events stay inert');
  assert.equal(document.getElementById('bgActivityChip'), null);
  assert.equal(document.getElementById('bgActivityPopover'), null);
  for (const type of ['click', 'visibilitychange', 'DOMContentLoaded']) {
    assert.equal(listeners.get(type)?.size || 0, 0, type + ' listener released');
  }
  console.log('PASS lifecycle, elapsed leaves, single-flight and hidden budgets');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.skipif(not _node_deps_available(), reason='node + jsdom unavailable')
def test_background_activity_elapsed_and_lifecycle_budget():
    proc = _run(_runtime_section('background-activity.js'), _LIFECYCLE_HARNESS)
    assert proc.returncode == 0, f'{proc.stderr}\n{proc.stdout}'
    assert 'PASS lifecycle' in proc.stdout
