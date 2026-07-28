"""jsdom test for the tool-row debug panel (request | post-tool state tabs).

The panel mounted by the `</> R{n}` entry next to a tool row.

2026-07-28 owner directive — TWO contract changes pinned here:
  1. ROUND-SCOPED: each tab renders ONLY what that round appended to the
     conversation (the increment over the previous round's same-kind
     payload), never the full conversation-history dump. "records only for
     this round of tool calls would be sufficient."
  2. NO cross-round chip strip: the in-panel navigation was ineffective, so
     it is gone. One click answers one round; the drawer remains the place
     for cross-round navigation.

Kept contract (the P7 baseline):
  • The panel mounts INLINE right after the tool slot, fetches payloads with
    kind='state' for the state tab, and renders through the SHARED debug
    renderer (renderDebugBlocksInto / updateDebugToolsBlock).
  • Single instance: reopening replaces the previous panel.
  • Live accelerator: an SSE-recorded state mirror needs NO network fetch.
  • Fallback: no tool slot in the DOM → drawer detail (kind=state).

NEUTERs:
  1. Drop the kind='state' argument → the state-kind pin flips red.
  2. Drop the round-scoping call → the previous round's history leaks back
     into the panel and the increment pin flips red.
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
  'ri.toolAnchorTip': 'View request that generated this tool (Round {round})',
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

/* Payloads engineered for prefix math: every round shares the SAME leading
 * history (SYS + HIST-u1). Round 2's request appends NEW-a2/NEW-t2; a state
 * mirror appends STATE-aN/STATE-tN on top of its round's request payload. */
const HIST = [
  { role: 'system', content: 'SYS' },
  { role: 'user', content: 'HIST-u1' },
];
function reqMsgs(roundNum) {
  const base = HIST.slice();
  if (Number(roundNum) >= 2)
    base.push({ role: 'assistant', content: 'NEW-a2' },
              { role: 'tool', content: 'NEW-t2' });
  return base;
}
function stateMsgs(roundNum) {
  return reqMsgs(roundNum).concat([
    { role: 'assistant', content: 'STATE-a' + roundNum },
    { role: 'tool', content: 'STATE-t' + roundNum },
  ]);
}

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
      const msgs = (kind === 'state') ? stateMsgs(roundNum) : reqMsgs(roundNum);
      return { taskId, roundNum, turn: turn || '', kind: kind || 'request',
        model: 'm', params: {}, label: 'R' + roundNum, tools: [],
        messages: msgs.map((m) => ({ ...m })) };
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
 * → colorJson) — expand EVERY block, THEN read the text. */
function expandAll(root) {
  const hs = root ? root.querySelectorAll('.debug-msg-header') : [];
  hs.forEach((h) => h.onclick());
  return hs.length;
}

(async () => {
  /* ── 1. Inline mount next to the tool slot — STATE tab, round-scoped ── */
  await openStateInspector('task-T1', 2);
  await sleep(30);
  const slot = document.querySelector('[data-prn="1"]');
  const panel = document.querySelector('.ri-state-panel');
  check('panel_mounted', !!panel);
  check('panel_after_slot', !!panel && slot.nextElementSibling === panel);
  check('payload_fetched_as_state_kind',
    CALLS.payloads.some(p => p.roundNum === '2' && p.kind === 'state'));
  const sbody = panel && panel.querySelector('.ri-state-body');
  if (sbody) expandAll(sbody);
  const sText = sbody ? sbody.textContent : '';
  /* State R2 = [SYS, HIST-u1 | NEW-a2, NEW-t2, STATE-a2, STATE-t2]; the
   * shared prefix with state R1 is 2, so ONLY the 4 new messages render. */
  check('state_body_shows_increment', sText.indexOf('STATE-t2') !== -1);
  check('state_body_hides_history', sText.indexOf('HIST-u1') === -1);
  check('no_state_strip',
    !!panel && !panel.querySelector('.ri-state-chip') &&
    !panel.querySelector('.ri-state-strip'));

  /* ── 2. Request tab — same round, request-axis increment ── */
  panel.querySelector('[data-ri-tab="request"]').onclick();
  await sleep(30);
  const rbody = panel.querySelector('.ri-state-body');
  if (rbody) expandAll(rbody);
  const rText = rbody ? rbody.textContent : '';
  check('request_tab_shows_increment', rText.indexOf('NEW-a2') !== -1);
  check('request_tab_hides_history', rText.indexOf('HIST-u1') === -1);
  check('request_tab_hides_state', rText.indexOf('STATE-t2') === -1);
  check('scoping_fetches_prev_round',
    CALLS.payloads.some(p => p.roundNum === '1'));

  /* ── 3. Single instance: reopening replaces the panel ── */
  await openStateInspector('task-T1', 2);
  await sleep(30);
  check('single_panel_instance',
    document.querySelectorAll('.ri-state-panel').length === 1);

  /* ── 4. Live accelerator: SSE-recorded state mirrors need NO fetch ── */
  showMessagesInDebug([{ role: 'user', content: 'live-state-acc-prev' }],
    'Round 2 工具结果后', true, 'conv-1', undefined, undefined,
    { kind: 'state', model: 'm-x', roundNum: 2, taskId: 'task-T1' });
  showMessagesInDebug([{ role: 'user', content: 'live-state-acc' }],
    'Round 3 工具结果后', true, 'conv-1', undefined, undefined,
    { kind: 'state', model: 'm-x', roundNum: 3, taskId: 'task-T1' });
  const fetchesBefore = CALLS.payloads.length;
  await openStateInspector('task-T1', 3);
  await sleep(30);
  check('accelerator_state_no_fetch', CALLS.payloads.length === fetchesBefore);
  const panel3 = document.querySelector('.ri-state-panel');
  check('accelerator_state_rendered',
    !!panel3 && expandAll(panel3.querySelector('.ri-state-body')) > 0 &&
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
  const dbg = document.getElementById('debugContent');
  check('fallback_renders_in_drawer',
    expandAll(dbg) > 0 && dbg.innerHTML.indexOf('STATE-t7') !== -1);

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
    assert not fails, 'tool-row debug panel failures:\n' + output
    assert output.count('PASS') >= 17, f'expected >=17 PASS, got:\n{output}'
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


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_neuter_scoping_dropped_leaks_history():
    """NC: render the FULL payload instead of this round's increment → the
    previous round's history leaks back into the panel and the round-scoping
    pin MUST fail. The panel exists to answer ONE round, not to dump the
    whole conversation (owner, 2026-07-28)."""
    shipped = os.path.join(JS_DIR, 'core', 'request_inspector.js')
    with open(shipped, encoding='utf-8') as f:
        src = f.read()
    anchor = ("const scoped = await _riRoundScopedMessages(taskId, roundNum, tab,\n"
              "    payload.messages);")
    assert anchor in src, 'NC anchor drifted — update the neuter'
    neutered = src.replace(anchor, 'const scoped = payload.messages;', 1)
    assert neutered != src
    tmp = os.path.join(HERE, '_request_inspector_p7_neutered.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(neutered)
    try:
        _run(ri_src_path=tmp, expect_fail='state_body_hides_history')
    finally:
        os.remove(tmp)
    with open(shipped, encoding='utf-8') as f:
        assert f.read() == src, 'shipped request_inspector.js must be byte-identical'


def test_state_inspector_wiring_pins():
    """Static pins: the inline entry, the row addressing, the i18n strings,
    the styles, and the api.js kind plumbing — the pieces a future refactor
    could silently drop while keeping every jsdom test green.

    The 2026-07-28 owner directive ADDED two anti-regression pins: the
    round-scoping helper must exist (the panel answers ONE round), and the
    removed cross-round chip strip must not creep back."""
    ri = open(os.path.join(JS_DIR, 'core', 'request_inspector.js'),
              encoding='utf-8').read()
    assert 'function openStateInspector' in ri
    assert 'function openToolDebugPanel' in ri, (
        'the merged single tool-row debug entry is gone')
    assert '_riMountToolPanel' in ri
    assert '_riRoundScopedMessages' in ri, (
        'the round-scoping helper is gone — the panel dumps full history again')
    assert 'ri-state-strip' not in ri, 'the cross-round strip crept back'
    assert "onclick = () => openStateInspector(taskId, s.roundNum)" in ri, (
        'drawer state rows must stay navigable')
    tr = open(os.path.join(JS_DIR, 'ui', 'tool_rounds.js'), encoding='utf-8').read()
    assert 'data-ri-state=' in tr, 'tool rows lost their state-mirror address'
    assert 'openToolDebugPanel(' in tr, (
        'tool rows lost their debug entry wiring')
    api = open(os.path.join(JS_DIR, 'api.js'), encoding='utf-8').read()
    assert 'getRequestPayload: (taskId, roundNum, turn, kind)' in api
    i18n = open(os.path.join(JS_DIR, 'i18n.js'), encoding='utf-8').read()
    for key in ("'ri.tabRequest'", "'ri.tabState'", "'ri.stateRowTip'",
                "'ri.stateEmpty'", "'ri.stateClose'"):
        assert key in i18n, f'{key} missing'
    css = open(os.path.join(ROOT, 'static', 'styles.css'), encoding='utf-8').read()
    assert '.ri-state-panel' in css
    assert '.ri-state-chip' not in css, 'the removed chip strip styles crept back'
    assert '.ri-panel-tab' in css


if __name__ == '__main__':
    print(_run())
