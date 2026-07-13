"""tests/test_frontend_autopilot_summary_anchor_placement.py — the autopilot
close-out REPORT is placed by the BACKEND-RESOLVED ``record.anchorMsgId``, a
pure lookup — NOT a frontend scan of ``_autopilotRunId`` stamps.

WHY (the root cause — reports stacking)
---------------------------------------
The old ``_apSummaryPlacements`` scanned messages for each run's ``_autopilotRunId``
stamp to compute a boundary index (``_runBoundaryIdx``). When a run's stamped
turn wasn't in the loaded window it fell to a ``{tail:true}`` placement — and
EVERY such run tail-docked, so reports from DIFFERENT runs piled up together at
the transcript tail. Placement was 100% frontend inference of a fact the backend
owns.

THE FIX
-------
The backend stamps ``record.anchorMsgId`` = the stable ``_msgId`` of the run's
boundary turn. ``_apSummaryPlacements`` becomes a pure LOOKUP: find the message
with that ``_msgId``, dock the panel after its ``#msg-<idx>`` element. The
stamp-scanning heuristic is deleted. The ts-ordered tail branch survives ONLY as
a genuine last resort — for a run whose anchor message is NOT in the loaded
window (compaction / lazy window). Because every LOADED run resolves to its own
distinct anchor, two distinct loaded runs can NEVER merge onto one tail stack.

Cases (drive the REAL shipped _apSummaryPlacements via jsdom):
  A (the reported bug, now fixed): two concluded runs, BOTH anchors loaded →
    TWO placements at TWO distinct anchor indices, neither tail. In the DOM,
    two distinct panels dock after two distinct #msg nodes.
  B (anchor missing → last resort): a run whose anchorMsgId is NOT among the
    loaded messages → exactly ONE tail placement. A second run WITH a loaded
    anchor still docks at its anchor (the missing one can't drag the resolvable
    one into a shared tail stack).
  C (determinism): every loaded run gets a NON-tail placement; the tail branch
    is empty when all anchors are loaded (proves tail is last-resort, not the
    common path).
  D (legacy record, no anchorMsgId): falls back to the ts-tail (backward compat
    for pre-fix records) — but a SINGLE such run, so no stacking of distinct
    runs is introduced by the fallback.

NEGATIVE CONTROL
  • NC (revert to _msgId-stamp scan): replace the anchor lookup with "dock every
    run at the tail" (the pre-fix inference) → Case A's two runs both tail-dock
    → the "two distinct anchors" assertion FAILS, proving the anchor lookup is
    load-bearing.

Skips cleanly when node/jsdom isn't installed.
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
ESCAPE_HTML = os.path.join(JS_DIR, 'core', 'escape_html.js')
SAFE_HTML = os.path.join(JS_DIR, 'core', 'safe_html.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[5];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

let _activeConv = { id: 'c', messages: [] };
win.activeStreams = global.activeStreams = new Map();
win.activeConvId = global.activeConvId = 'c';
win.getActiveConv = global.getActiveConv = () => _activeConv;
win.t = global.t = (k) => k;
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.convAutoTranslate = global.convAutoTranslate = () => false;

const CHAT = fs.readFileSync(process.argv[2], 'utf8');
const NC = process.argv[6] || '';
let chatSrc = CHAT;
if (NC === 'nc_tail_scan') {
  // Revert to the pre-fix inference: ignore the backend anchor and dock EVERY
  // run at the transcript tail (what happened when stamp-scan couldn't find a
  // boundary). The two distinct runs then collapse onto one tail stack.
  chatSrc = CHAT.replace(
    'const idx = anchorId ? idxByMsgId.get(anchorId) : undefined;',
    'const idx = undefined; /* neutered: ignore backend anchor */');
}
const _applied = (NC === '') || (chatSrc !== CHAT);
check('nc_pattern_applied', _applied);

(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // safe_html.js
// chat_render's report panel calls readTranslation() from core/translation_model.js
// (a load-order dep guaranteed by _BUNDLE_FILES: translation_model precedes
// chat_render). Eval it here so _apReportPanelHTML doesn't ReferenceError and
// get swallowed by its non-fatal try/catch — mirrors the sibling autopilot
// harnesses (test_frontend_autopilot_report_affordance.py).
(0, eval)(fs.readFileSync(process.argv[3].replace('escape_html.js', 'translation_model.js'), 'utf8'));  // core/translation_model.js
(0, eval)(chatSrc);                                   // chat_render.js (real / neutered)

if (typeof _apSummaryPlacements !== 'function' || typeof _applyAutopilotSummaryPanels !== 'function') {
  console.log('FAIL fn_exposed'); process.exit(0);
}
check('fn_exposed', true);

const inner = win.document.getElementById('chatInner');
function paintRows(conv) {
  inner.innerHTML = '';
  conv.messages.forEach((m, i) => {
    const d = win.document.createElement('div');
    d.id = 'msg-' + i;
    if (m && m._msgId) d.setAttribute('data-msg-id', m._msgId);
    d.className = 'message ' + (m.role === 'assistant' ? 'assistant' : 'user');
    inner.appendChild(d);
  });
}
function vu(runId, msgId) { return { role: 'user', _isVirtualUser: true, _autopilotRunId: runId, _msgId: msgId, content: 'go on' }; }
function agent(msgId, txt) { return { role: 'assistant', _msgId: msgId, content: txt || 'reply' }; }
function human(msgId, txt) { return { role: 'user', _msgId: msgId, content: txt || 'human' }; }
// A concluded record carrying the backend-resolved anchor.
function rep(runId, anchorMsgId, ts) { return { runId, status: 'concluded', reason: 'task_done', content: '# Report ' + runId, anchorMsgId, ts: ts || 1 }; }

// ── Case A — two loaded runs → two distinct anchors, neither tail (THE BUG) ──
{
  _activeConv = { id: 'cA', messages: [
      human('m0', 'obj'),
      vu('R1', 'm1'), agent('m2', 'a-R1'),
      vu('R2', 'm3'), agent('m4', 'a-R2')],
    autopilotSummaries: {
      R1: rep('R1', 'm2', 10),
      R2: rep('R2', 'm4', 20) } };
  const pl = _apSummaryPlacements(_activeConv);
  const byRun = {}; pl.forEach(p => { byRun[p.runId] = p; });
  check('A_two_placements', pl.length === 2);
  check('A_R1_anchor', byRun.R1 && byRun.R1.afterMsgIdx === 2 && !byRun.R1.tail);
  check('A_R2_anchor', byRun.R2 && byRun.R2.afterMsgIdx === 4 && !byRun.R2.tail);
  check('A_distinct_anchors', byRun.R1 && byRun.R2 && byRun.R1.afterMsgIdx !== byRun.R2.afterMsgIdx);
  // DOM: two distinct panels docked after two distinct #msg nodes.
  paintRows(_activeConv);
  _applyAutopilotSummaryPanels(inner, _activeConv);
  const panels = inner.querySelectorAll(':scope > details.ap-summary-panel');
  check('A_two_panels_in_dom', panels.length === 2);
  const p1 = win.document.getElementById('msg-2').nextElementSibling;
  const p3 = win.document.getElementById('msg-4').nextElementSibling;
  check('A_panel_after_m2', p1 && p1.classList && p1.classList.contains('ap-summary-panel') && p1.getAttribute('data-ap-report-run') === 'R1');
  check('A_panel_after_m4', p3 && p3.classList && p3.classList.contains('ap-summary-panel') && p3.getAttribute('data-ap-report-run') === 'R2');
  check('A_panels_not_adjacent', p1 !== p3);
}

// ── Case B — one anchor missing (last resort) + one anchor loaded ──
{
  _activeConv = { id: 'cB', messages: [
      human('m0', 'obj'),
      vu('R2', 'm3'), agent('m4', 'a-R2')],
    autopilotSummaries: {
      R1: rep('R1', 'm-gone', 10),   // anchor NOT in the loaded window
      R2: rep('R2', 'm4', 20) } };
  const pl = _apSummaryPlacements(_activeConv);
  const byRun = {}; pl.forEach(p => { byRun[p.runId] = p; });
  check('B_two_placements', pl.length === 2);
  check('B_R1_is_tail', byRun.R1 && byRun.R1.tail === true);
  check('B_R2_anchored_not_tail', byRun.R2 && byRun.R2.afterMsgIdx === 2 && !byRun.R2.tail);
}

// ── Case C — determinism: all anchors loaded → NO tail placement at all ──
{
  _activeConv = { id: 'cC', messages: [
      vu('R1', 'm0'), agent('m1'),
      vu('R2', 'm2'), agent('m3'),
      vu('R3', 'm4'), agent('m5')],
    autopilotSummaries: {
      R1: rep('R1', 'm1', 10), R2: rep('R2', 'm3', 20), R3: rep('R3', 'm5', 30) } };
  const pl = _apSummaryPlacements(_activeConv);
  check('C_three_placements', pl.length === 3);
  check('C_no_tail_when_all_anchors_loaded', pl.every(p => !p.tail));
  const idxs = pl.map(p => p.afterMsgIdx).sort((a,b)=>a-b);
  check('C_distinct_indices', idxs[0] === 1 && idxs[1] === 3 && idxs[2] === 5);
}

// ── Case D — legacy record with NO anchorMsgId → ts-tail fallback (compat) ──
{
  const legacy = { runId: 'R1', status: 'concluded', reason: 'task_done', content: '# Legacy', ts: 5 };
  _activeConv = { id: 'cD', messages: [human('m0', 'obj'), vu('R1', 'm1'), agent('m2')],
    autopilotSummaries: { R1: legacy } };
  const pl = _apSummaryPlacements(_activeConv);
  check('D_one_placement', pl.length === 1);
  check('D_legacy_tail', pl[0] && pl[0].runId === 'R1' && pl[0].tail === true);
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_ap_anchor_placement_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, CHAT_RENDER, ESCAPE_HTML, SAFE_HTML, ROOT, nc],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_two_loaded_runs_dock_at_distinct_anchors():
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'anchor-placement failures:\n' + output
    assert output.count('PASS') >= 16, f'expected >=16 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_tail_scan_stacks_distinct_runs():
    """NC: ignoring the backend anchor (dock-at-tail inference) must collapse the
    two distinct loaded runs — proving the anchor lookup is load-bearing."""
    output = _run('nc_tail_scan')
    assert 'PASS nc_pattern_applied' in output, f'NC mutation did not apply:\n{output}'
    assert 'FAIL A_distinct_anchors' in output, (
        'Reverting to dock-at-tail did NOT stack the two runs — the anchor '
        f'lookup is not load-bearing:\n{output}')


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
    else:
        test_two_loaded_runs_dock_at_distinct_anchors()
        test_nc_tail_scan_stacks_distinct_runs()
        print('PASS test_frontend_autopilot_summary_anchor_placement')
