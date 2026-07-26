"""jsdom test for Request Inspector P7 — INLINE state inspector.

The owner's interaction complaint: the drawer's state-mirror list ("Round N
工具结果后 · 6 msgs") was a dead end — clicking it did nothing, and even when
it worked the content appeared in a far-away drawer instead of next to the
tool call whose execution the mirror captures.

P7 makes the state axis navigable in place:
  1. Every tool row addresses its post-tool STATE mirror via
     data-ri-state="taskId:roundNum" (same roundNum axis as the producing
     request — design §3.1) and carries an S-anchor that opens the mirror
     INLINE, mounted as a .ri-state-panel right after the tool slot.
  2. The drawer state rows become NAVIGATION: clicking one jumps to the
     tool slot and opens the inline panel; when the slot is not in the DOM
     (unloaded/old conversation), it degrades to the drawer detail pane
     (kind=state) instead of a dead click.
  3. The panel renders through the SHARED debug renderer
     (renderDebugBlocksInto / updateDebugToolsBlock) — no second JSON
     renderer — and offers a chip strip of all state mirrors of the task
     for prev/next navigation without leaving the chat.
  4. Payloads fetch with kind='state' (network) and the live accelerator's
     .states log wins over the network when fresh.

Covered:
  1. openStateInspector mounts the panel right after the tool slot, fetches
     the payload with kind='state', and renders the state messages.
  2. The chip strip lists all state mirrors and switches the active round.
  3. Single instance: reopening replaces the previous panel.
  4. Live accelerator: an SSE-recorded state mirror needs NO network fetch.
  5. Fallback: no tool slot in the DOM → drawer opens and renders the
     state payload through showMessagesInDebug.

NEUTER: drop the kind='state' argument from the network fetch → the
state-kind pin flips red, proving the payload comes from the STATE axis
(post-tool mirrors), not the request axis.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="riDrawer" style="display:none">' +
  '  <div id="riTaskList"></div><div id="riRoundList"></div>' +
  '  <div class="debug-panel" id="debugPanel">' +
  '    <div id="debugTitle"></div><div id="debugContent"></div>' +
  '  </div>' +
  '</div>' +
  '<div id="chatinner">' +
  '  <div data-prn="1"><div class="ri-tool-anchor-row" data-ri-state="task-T1:2"></div></div>' +
  '  <div data-prn="2"><div class="ri-tool-anchor-row" data-ri-state="task-T1:3"></div></div>' +
  '</div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

global.escapeHtml = win.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
global.Icon = win.Icon = (n, s) => `<svg data-icon="${n}"></svg>`;
const _I18N = {
  'ri.empty': 'No tasks', 'ri.loading': 'Loading…', 'ri.expired': 'expired',
  'ri.requests': 'requests', 'ri.live': 'live', 'ri.states': 'States',
  'ri.stateNote': 'not requests', 'ri.coveragePartial': 'partial',
  'ri.coverageAmbiguous': 'ambiguous',
  'ri.turnPlanning': 'Planner', 'ri.turnWorking': 'Worker',
  'ri.turnReviewing': 'Critic',
  'ri.prefixFold': 'prefix {k} vs {base}',
  'ri.stateAnchorTip': 'state after round {round}',
  'ri.stateRowTip': 'jump to the tool call',
  'ri.stateEmpty': 'State mirror expired or missing',
  'ri.stateClose': 'Close state inspector',
};
global.t = win.t = (k, a) => {
  let s = _I18N[k] || k;
  if (a) for (const kk of Object.keys(a)) s = s.replace('{' + kk + '}', String(a[kk]));
  return s;
};
global.activeConvId = win.activeConvId = 'conv-1';
global.debugVisible = win.debugVisible = false;
global._featureFlags = win._featureFlags = { debug_mode: true };
global.conversations = win.conversations = [{ id: 'conv-1', messages: [] }];

const CALLS = { getRequests: 0, payloads: [] };
win.Api = global.Api = {
  tasks: {
    byConv: async () => ({ convId: 'conv-1', tasks: [
      { taskId: 'task-T1', status: 'done', createdAt: 1, completedAt: 2,
        live: false, requestCount: 3, stateCount: 2, legacyCount: 0,
        hasEvents: true }] }),
    getRequests: async (taskId) => {
      CALLS.getRequests++;
      return { taskId, eventsAvailable: true, coverage: 'full', requestCount: 3,
        requests: [
          { roundNum: 1, ts: 1, model: 'm', params: {}, messageCount: 2,
            toolsCount: 1, approxTokens: 10, label: 'R1', turn: '', legacy: false, attempts: [] },
          { roundNum: 2, ts: 2, model: 'm', params: {}, messageCount: 4,
            toolsCount: 1, approxTokens: 20, label: 'R2', turn: '', legacy: false, attempts: [] },
        ],
        states: [
          { roundNum: 1, label: 'Round 1 工具结果后', messageCount: 3, ts: 3, legacy: false },
          { roundNum: 2, label: 'Round 2 工具结果后', messageCount: 5, ts: 4, legacy: false },
        ] };
    },
    getRequestPayload: async (taskId, roundNum, turn, kind) => {
      CALLS.payloads.push({ roundNum: String(roundNum), turn: turn || '', kind: kind || '' });
      return { taskId, roundNum, turn: turn || '', kind: kind || 'request',
        model: 'm', params: {}, label: 'R' + roundNum, tools: [],
        messages: [{ role: 'user', content: 'state-payload-R' + roundNum }] };
    },
  },
  clientError: { report: () => {} },
};

const debugSrc = fs.readFileSync(path.join(ROOT,'static','js','core','debug_panel.js'), 'utf8');
const riSrc    = fs.readFileSync(path.join(ROOT,'static','js','core','request_inspector.js'), 'utf8');
eval(debugSrc + '\n' + riSrc);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
/* createBlock lazy-renders the message body on first expand (header.onclick
 * → colorJson) — mirror the drawer UX: expand the block, THEN read text. */
function expandFirst(root) {
  const h = root && root.querySelector('.debug-msg-header');
  if (h) h.onclick();
  return !!h;
}

(async () => {
  /* ── 1. Inline mount next to the tool slot ── */
  await openStateInspector('task-T1', 2);
  await sleep(30);
  const slot = document.querySelector('[data-prn="1"]');
  const panel = document.querySelector('.ri-state-panel');
  check('panel_mounted', !!panel);
  check('panel_after_slot', !!panel && slot.nextElementSibling === panel);
  check('payload_fetched_as_state_kind',
    CALLS.payloads.some(p => p.roundNum === '2' && p.kind === 'state'));
  check('state_body_rendered',
    !!panel && expandFirst(panel.querySelector('.ri-state-body')) &&
    panel.querySelector('.ri-state-body')
      .innerHTML.indexOf('state-payload-R2') !== -1);
  check('strip_chips_rendered',
    !!panel && panel.querySelectorAll('.ri-state-chip').length === 2);
  const selChip = panel && panel.querySelector('.ri-state-chip.ri-sel');
  check('active_chip_marked', !!selChip && selChip.dataset.round === '2');

  /* ── 2. Chip strip navigates without leaving the chat ── */
  panel.querySelectorAll('.ri-state-chip')[0].onclick();
  await sleep(30);
  check('chip_nav_switches_round',
    expandFirst(panel.querySelector('.ri-state-body')) &&
    panel.querySelector('.ri-state-body')
      .innerHTML.indexOf('state-payload-R1') !== -1);
  const selChip2 = panel.querySelector('.ri-state-chip.ri-sel');
  check('chip_nav_moves_active', !!selChip2 && selChip2.dataset.round === '1');

  /* ── 3. Single instance: reopening replaces the panel ── */
  await openStateInspector('task-T1', 2);
  await sleep(30);
  check('single_panel_instance',
    document.querySelectorAll('.ri-state-panel').length === 1);

  /* ── 4. Live accelerator: SSE-recorded state mirror needs NO fetch ── */
  showMessagesInDebug([{ role: 'user', content: 'live-state-acc' }],
    'Round 3 工具结果后', true, 'conv-1', undefined, undefined,
    { kind: 'state', model: 'm-x', roundNum: 3, taskId: 'task-T1' });
  const fetchesBefore = CALLS.payloads.length;
  await openStateInspector('task-T1', 3);
  await sleep(30);
  check('accelerator_state_no_fetch', CALLS.payloads.length === fetchesBefore);
  const panel3 = document.querySelector('.ri-state-panel');
  check('accelerator_state_rendered',
    !!panel3 && expandFirst(panel3.querySelector('.ri-state-body')) &&
    panel3.querySelector('.ri-state-body')
      .innerHTML.indexOf('live-state-acc') !== -1);

  /* ── 5. Fallback: tool slot not in the DOM → drawer detail (kind=state) ── */
  check('drawer_still_closed_before_fallback',
    !document.body.classList.contains('ri-open'));
  await openStateInspector('task-T1', 7);   // no data-ri-state marker for 7
  await sleep(30);
  check('fallback_opens_drawer', document.body.classList.contains('ri-open'));
  check('fallback_fetches_state_kind',
    CALLS.payloads.some(p => p.roundNum === '7' && p.kind === 'state'));
  check('fallback_renders_in_drawer',
    expandFirst(document.getElementById('debugContent')) &&
    document.getElementById('debugContent').innerHTML
      .indexOf('state-payload-R7') !== -1);

  console.log(out.join('\n'));
})().catch(e => { console.log('FAIL harness_exception ' + (e && e.stack || e)); });
"""


def _run(ri_src_path=None, expect_fail=None):
    harness = os.path.join(HERE, '_ri_p7_harness.js')
    src = _HARNESS
    if ri_src_path:
        # The neuter run drives a MODIFIED copy of request_inspector.js.
        src = src.replace(
            "path.join(ROOT,'static','js','core','request_inspector.js')",
            "process.argv[2]")
    with open(harness, 'w') as f:
        f.write(src)
    try:
        proc = subprocess.run(
            ['node', harness, ri_src_path or '', ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    if expect_fail:
        assert f'FAIL {expect_fail}' in output, (
            f'neutered copy did NOT flip {expect_fail} red:\n{output}')
        return output
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'P7 inline-state failures:\n' + output
    assert output.count('PASS') >= 14, f'expected >=14 PASS, got:\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_p7_inline_state_inspector():
    _run()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_neuter_state_kind_dropped_flips_red():
    """NC: drop the kind='state' argument from the payload fetch → the
    state-kind pin MUST fail. Without it the inline panel would silently
    render the PRE-REQUEST snapshot (the request axis) while claiming to
    show the post-tool state — the exact off-axis confusion this feature
    exists to kill."""
    shipped = os.path.join(JS_DIR, 'core', 'request_inspector.js')
    with open(shipped, encoding='utf-8') as f:
        src = f.read()
    anchor = "kind === 'state' ? 'state' : undefined"
    assert anchor in src, 'NC anchor drifted — update the neuter'
    neutered = src.replace(anchor, 'undefined', 1)
    assert neutered != src
    tmp = os.path.join(HERE, '_request_inspector_p7_neutered.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(neutered)
    try:
        _run(ri_src_path=tmp, expect_fail='payload_fetched_as_state_kind')
    finally:
        os.remove(tmp)
    with open(shipped, encoding='utf-8') as f:
        assert f.read() == src, 'shipped request_inspector.js must be byte-identical'


def test_state_inspector_wiring_pins():
    """Static pins: the inline entry, the row addressing, the i18n strings,
    the styles, and the api.js kind plumbing — the pieces a future refactor
    could silently drop while keeping every jsdom test green."""
    ri = open(os.path.join(JS_DIR, 'core', 'request_inspector.js'),
              encoding='utf-8').read()
    assert 'function openStateInspector' in ri
    assert '_riMountStatePanel' in ri
    assert "onclick = () => openStateInspector(taskId, s.roundNum)" in ri, (
        'drawer state rows must stay navigable')
    tr = open(os.path.join(JS_DIR, 'ui', 'tool_rounds.js'), encoding='utf-8').read()
    assert 'data-ri-state=' in tr, 'tool rows lost their state-mirror address'
    assert 'openStateInspector(' in tr
    api = open(os.path.join(JS_DIR, 'api.js'), encoding='utf-8').read()
    assert 'getRequestPayload: (taskId, roundNum, turn, kind)' in api
    i18n = open(os.path.join(JS_DIR, 'i18n.js'), encoding='utf-8').read()
    for key in ("'ri.stateAnchorTip'", "'ri.stateRowTip'", "'ri.stateEmpty'",
                "'ri.stateClose'"):
        assert key in i18n, f'{key} missing'
    css = open(os.path.join(ROOT, 'static', 'styles.css'), encoding='utf-8').read()
    assert '.ri-state-panel' in css
    assert '.ri-state-chip' in css


if __name__ == '__main__':
    print(_run())
