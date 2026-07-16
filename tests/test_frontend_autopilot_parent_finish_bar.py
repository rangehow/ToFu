"""jsdom regression for the autopilot parent-worker finish-bar completeness.

WHY
---
When autopilot kicks in, the frontend EARLY-FINALIZES the parent worker bubble
at `autopilot_vu_start` (streaming_render.js `_beginVuStreaming`) so the VU can
own the single `#streaming-msg` substrate. But the parent worker's SETTLED
finish metadata (finishReason / usage / cost) arrives ONLY on the parent `done`
SSE event — which the backend deliberately WITHHOLDS until the entire VU stream
completes (so the follow-up baton can ride on it, see
lib/tasks_pkg/orchestrator/_finalize.py + autopilot.py). Result: for the WHOLE
VU turn (12–52s, measured) the parent bubble's finish bar carries ONLY the model
tag — no tokens, no cost, no ✓ finishReason. The user reported this as "the
finish tag bar for the previous agent's result is always incomplete … it only
appears complete after the autopilot bubble suddenly disappears" (== the late
`done` finally landing).

THE FIX (this suite locks it in): the backend now attaches the parent's already-
committed dict (`task['_committedMsg']`, the EXACT record it wrote to
conversations.messages, carrying finishReason/usage/cost) onto the
`autopilot_vu_start` event as `parentMessage`. `_beginVuStreaming` projects those
fields onto the parent assistant BEFORE finalizing it, so the bar is COMPLETE at
handoff. The later `done` still ships the authoritative copy (a harmless no-op
repaint), and the backend skip path (no `parentMessage`) still falls back to the
`done` re-render — both preserved.

This harness drives the REAL shipped `_handleAutopilotVuEvent` (streaming_render.js)
under jsdom: it seeds a parent worker assistant, fires `autopilot_vu_start` with
`parentMessage`, and asserts the parent assistant object now carries
finishReason + usage + cost. NC mode strips `parentMessage` → the parent stays
finish-bar-incomplete (model only), proving the projection is what fills the bar.
Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NC = process.argv[3] === 'NC';   // negative-control: strip parentMessage
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

win.t = global.t = (k) => k;
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.formatClockTime = global.formatClockTime = () => '12:00';
win.raw = global.raw = (s) => ({ _raw: String(s), toString() { return String(s); } });
win.safeHtml = global.safeHtml = function (strings, ...vals) {
  let out = '';
  strings.forEach((s, i) => {
    out += s;
    if (i < vals.length) {
      const v = vals[i];
      out += (v && v._raw !== undefined) ? v._raw : win.escapeHtml(v);
    }
  });
  return { toString() { return out; } };
};
win.renderMarkdown = global.renderMarkdown = (s) => String(s == null ? '' : s);

win.activeConvId = global.activeConvId = 'C1';
// Seed a parent worker assistant that just stopped streaming — it has content
// but NO finishReason/usage/cost yet (the exact state at autopilot handoff,
// before the withheld `done` event lands).
const parentAssistant = {
  role: 'assistant',
  _msgId: 'worker-1',
  content: 'Here is the worker reply.',
  thinking: '',
  toolRounds: [],
  model: 'aws.claude-opus-4.8',
  // frontend-local field that MUST survive the verbatim projection
  _translateDone: true,
};
win.conversations = global.conversations = [{ id: 'C1', messages: [parentAssistant] }];

// Streaming substrate + no-op helpers _beginVuStreaming / updateStreamingUI touch.
const streamBufs = new Map();
win.streamBufs = global.streamBufs = streamBufs;
win.twStart = global.twStart = (cid) => { streamBufs.set(cid, { content: '', thinking: '', toolRounds: [], phase: null }); };
win.twUpdate = global.twUpdate = _noopUpdate;
function _noopUpdate() {}
win.twStop = global.twStop = (cid) => { streamBufs.delete(cid); };

const _noop = () => {};
const _noopStr = () => '';
for (const [name, fn] of [
  ['scrollToBottom', _noop], ['isNearBottom', () => false],
  ['buildTurnNav', _noop], ['saveConversations', _noop],
  ['renderMcpLoginHintHtml', _noopStr], ['renderTurnProvenanceHtml', _noopStr],
  ['renderPreferenceLearnedHtml', _noopStr], ['_buildSwarmInboxChipsHTML', _noopStr],
  ['_fcFingerprint', () => 0], ['_renderFileChangesHtml', _noopStr],
  ['_extractFileChangesFromRoundsAsync', () => Promise.resolve([])],
  ['normalizeErrorEnvelope', (x) => x],
  ['Icon', (n) => '<svg data-icon="' + n + '"></svg>'],
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = fn; }
}
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<svg id="critic-avatar"></svg>';

// A minimal ConvView so _beginVuStreaming's finalize call doesn't throw; we
// only care about the DATA projection onto parentAssistant, not the DOM swap.
win.ConvView = global.ConvView = {
  finalizeStreaming: function () { return true; },
  upsertMessage: function () { return true; },
  removeMessage: function () { return true; },
};

eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'streaming_render.js'), 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _handleAutopilotVuEvent !== 'function') {
  console.log('FAIL fn_exposed _handleAutopilotVuEvent missing'); process.exit(0);
}
check('fn_exposed', true);

// The SETTLED parent dict the backend stamps as task['_committedMsg'] and now
// ships on autopilot_vu_start as `parentMessage`.
const PARENT_MESSAGE = {
  role: 'assistant',
  _msgId: 'worker-1',
  content: 'Here is the worker reply.',
  thinking: '',
  toolRounds: [],
  model: 'aws.claude-opus-4.8',
  finishReason: 'end_turn',
  usage: { input_tokens: 4200, output_tokens: 380, cache_read_input_tokens: 3900 },
  cost: { costCny: 0.0123 },
  apiRounds: [{ round: 0, usage: { output_tokens: 380 } }],
};

// PRECONDITION: parent bar is INCOMPLETE — model only, no terminal signal.
check('precondition_no_finish', !parentAssistant.finishReason && !parentAssistant.usage);

const ev = { type: 'autopilot_vu_start', vuMsgId: 'vu-1' };
if (!NC) ev.parentMessage = PARENT_MESSAGE;   // NC drops it (skip-path simulation)
_handleAutopilotVuEvent('C1', ev);

// ── The bite: after handoff the parent assistant carries the SETTLED finish
//    metadata so renderFinishInfo will draw a COMPLETE bar (model + tokens +
//    cost + ✓ finishReason) immediately — not only after the late `done`. ──
check('parent_finishReason_set', parentAssistant.finishReason === 'end_turn');
check('parent_usage_set', !!(parentAssistant.usage && parentAssistant.usage.output_tokens === 380));
check('parent_cost_set', !!(parentAssistant.cost && parentAssistant.cost.costCny === 0.0123));
check('parent_apiRounds_set', Array.isArray(parentAssistant.apiRounds) && parentAssistant.apiRounds.length === 1);
// The bubble still gets marked for the authoritative `done` repaint.
check('vuTookOver_marked', parentAssistant._vuTookOverBubble === true);
// VERBATIM projection must not clobber frontend-local fields.
check('local_field_preserved', parentAssistant._translateDone === true);
// The VU bubble was created (handoff proceeded).
const conv = conversations.find(c => c.id === 'C1');
check('vu_bubble_created', !!conv.messages.find(m => m._msgId === 'vu-1' && m._isVirtualUser));

console.log(out.join('\n'));
"""

# Prepended into the harness: the worker's live `#streaming-msg` must exist at
# handoff (the projection block is guarded by `if (sm) {...}` — that's the real
# runtime state: autopilot fires the instant the worker turn stops streaming,
# while its `#streaming-msg` is still in the DOM). Inserted right before the
# `_handleAutopilotVuEvent` call.
_HARNESS = _HARNESS.replace(
    "const ev = { type: 'autopilot_vu_start', vuMsgId: 'vu-1' };",
    "document.getElementById('chatInner').insertAdjacentHTML('beforeend',\n"
    "  '<div id=\"streaming-msg\"><div id=\"streaming-body\"></div></div>');\n"
    "const ev = { type: 'autopilot_vu_start', vuMsgId: 'vu-1' };",
)


def _run(nc: bool):
    harness = os.path.join(HERE, '_autopilot_parent_finish_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        argv = ['node', harness, ROOT]
        if nc:
            argv.append('NC')
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
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
def test_vu_start_parent_message_completes_finish_bar():
    """autopilot_vu_start carrying `parentMessage` projects the settled
    finishReason/usage/cost onto the parent worker assistant at handoff, so its
    finish bar is complete immediately — not incomplete until the withheld
    parent `done` event lands after the whole VU stream."""
    output = _run(nc=False)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'parent-finish-bar failures:\n' + output
    # fn_exposed + precondition + 4 finish fields + vuTookOver + local_field
    # + vu_bubble_created = 9
    assert output.count('PASS') >= 9, f'expected >=9 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_no_parent_message_leaves_finish_bar_incomplete():
    """Negative control: when autopilot_vu_start carries NO `parentMessage`
    (the backend skip path — freshness/CAS-miss/inline), the parent assistant
    is NOT back-filled at handoff → its finish bar stays incomplete (model
    only) until the later `done` re-render. Proves the projection is what fills
    the bar, not the harness."""
    output = _run(nc=True)
    lines = output.splitlines()

    def _status(name):
        for ln in lines:
            if ln.endswith(' ' + name):
                return ln.split(' ', 1)[0]
        return None

    assert _status('parent_finishReason_set') == 'FAIL', \
        'NC (no parentMessage) must leave finishReason unset:\n' + output
    assert _status('parent_usage_set') == 'FAIL', \
        'NC must leave usage unset:\n' + output
    assert _status('parent_cost_set') == 'FAIL', \
        'NC must leave cost unset:\n' + output
    # The handoff still proceeds and marks the bubble for the `done` repaint —
    # that fallback path is intact (the bar fills later, not never).
    assert _status('vuTookOver_marked') == 'PASS', \
        'NC must still mark _vuTookOverBubble for the done re-render:\n' + output
    assert _status('vu_bubble_created') == 'PASS', \
        'NC handoff must still create the VU bubble:\n' + output
