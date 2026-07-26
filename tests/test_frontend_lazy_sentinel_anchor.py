"""tests/test_frontend_lazy_sentinel_anchor.py — chatInner message-ORDER guard.

WHY
---
Reported symptom: "the user message of the FIRST round is rendered at the very
BOTTOM of chatInner". Reproduced against the real shipped code — the DOM order
came out `m4…m23, m0, m1, m2, m3`, i.e. the conversation's OPENING turns sat
below its newest one.

It is a TWO-STEP chain, and neither step looks wrong on its own:

  1. `ui/chat_render.js` surgical reconcile — for the FIRST message of the
     reconcile window (`_prevEl` and `_cursor` both null) the insertion anchor
     used to fall back to `inner.firstChild`. But when a lazy window is active
     `firstChild` is `#_lazyLoadSentinel`, NOT a message. Anchoring there
     inserts message #1 ABOVE the sentinel, which pushes the sentinel down one
     slot — on EVERY background repaint. Cost / file-change / compaction data
     all land as background repaints on a conversation open, so after a few of
     them the sentinel has migrated to the very BOTTOM of `#chatInner`.
     Invisible on screen: the sentinel is a thin one-line strip.

  2. `ui/streaming_render.js::_loadOlderMessages` splices the older batch in
     with `sentinel.after(frag)`. With the sentinel now at the bottom, the
     OLDEST messages are inserted BELOW the newest one.

Step 1 only becomes visible when the reader scrolls up and step 2 fires. This
harness drives BOTH real functions end-to-end and asserts the two invariants:

  A. the head sentinel STAYS the first child across background repaints;
  B. after a real `_loadOlderMessages()`, DOM order == conv.messages order.

NEUTER: restore the `inner.firstChild` fallback (the pre-fix code) and prove
both assertions FAIL — i.e. the sentinel-skipping anchor is load-bearing.

`ui/streaming_render.js` and `ui/chat_render.js` are eval-ed as ONE
concatenated script, exactly as `lib/js_bundler.py` emits them, because their
top-level `let` bindings (`_lazyConvId` / `_lazyRenderedFrom` /
`_lazyRenderedTo` / `_INITIAL_RENDER`) share a single script scope. Eval-ing
them separately puts each file's `let` in its own scope and `renderChat` cannot
see them — the harness would fail for a reason unrelated to the invariant.

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
JS_DIR = os.path.join(ROOT, 'static', 'js')
ESCAPE_HTML = os.path.join(JS_DIR, 'core', 'escape_html.js')
SAFE_HTML = os.path.join(JS_DIR, 'core', 'safe_html.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')

# The shipped anchor expression the fix introduced, and the pre-fix expression
# the NEUTER restores. Kept as module constants so a rename breaks LOUDLY here
# rather than silently turning the NEUTER into a no-op.
_FIXED_ANCHOR = ('const _want = _prevEl ? _prevEl.nextSibling '
                 ': (_cursor ? _cursor.nextSibling : _headAnchor());')
_LEGACY_ANCHOR = ('const _want = _prevEl ? _prevEl.nextSibling '
                  ': (_cursor ? _cursor.nextSibling : inner.firstChild);')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[5];
const NC = process.argv[6] || '';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
global.setTimeout = win.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { if (typeof fn === 'function') fn(); return 0; };
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };
global.IntersectionObserver = win.IntersectionObserver = function () {
  return { observe(){}, unobserve(){}, disconnect(){} };
};

const out = [];
function check(name, cond, extra) { out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? (' ' + extra) : '')); }

// ── Idle conv: no live stream, so renderChat takes the static surgical path. ──
win.activeStreams = global.activeStreams = new Map();
win.activeConvId = global.activeConvId = 'c1';
win.t = global.t = (k) => k;
win._fmtAbsoluteDateTime = global._fmtAbsoluteDateTime = () => '';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
win.renderToolRoundsHTML = global.renderToolRoundsHTML = () => '';
win.renderSegmentTimelineHTML = global.renderSegmentTimelineHTML = () => '';
const _noop = () => '';
for (const name of [
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar',
  'renderErrorEnvelope','renderBranchZone','renderTurnCtxNote',
  'renderPreferenceLearnedHtml','renderFinishInfo','_buildSwarmInboxChipsHTML',
  '_injectAnchoredBranches','_stampFreshness','buildTurnNav','calcCostCny',
  '_forceScrollToBottom','scrollToBottom','isNearBottom','showStreamingUIForConv',
  '_captureScrollAnchor','_restoreScrollAnchor','_applyAutopilotRunFolds',
]) { if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; } }
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<img data-avatar="onigiri">';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<img data-avatar="worker">';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<img data-avatar="planner">';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<img data-avatar="critic">';
win.BASE_PATH = global.BASE_PATH = '';
win._prefetchConvCosts = global._prefetchConvCosts = () => ({ then: () => {} });
win._prefetchConvFileChanges = global._prefetchConvFileChanges = () => ({ then: () => {} });
win._editingMsgIdx = global._editingMsgIdx = null;
win._activeBranch = global._activeBranch = null;
win._openScrollConvId = global._openScrollConvId = null;
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';
// Never-equal fingerprint so Guard 2 never SKIPS the surgical re-render.
win._convRenderFingerprint = global._convRenderFingerprint =
  (c) => 'fp:' + (c ? c.messages.length : 0) + ':' + Math.random();

// jsdom has no layout engine — give the container the geometry
// _loadOlderMessages reads for its scroll compensation.
const _ct = win.document.getElementById('chatContainer');
Object.defineProperty(_ct, 'scrollHeight', { get: () => 5000, configurable: true });
_ct.scrollTop = 0;

let chatSrc = fs.readFileSync(process.argv[2], 'utf8');
const FIXED = process.argv[7];
const LEGACY = process.argv[8];
if (NC === 'firstchild') {
  // NEUTER: restore the pre-fix anchor — fall back to `inner.firstChild`,
  // which is the SENTINEL when a lazy window is active.
  if (chatSrc.indexOf(FIXED) === -1) {
    console.log('FAIL neuter_not_applied (fixed anchor sentinel absent)');
    console.log(out.join('\n')); process.exit(0);
  }
  chatSrc = chatSrc.replace(FIXED, LEGACY);
}

(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // safe_html.js
(0, eval)(fs.readFileSync(process.argv[3].replace('escape_html.js', 'translation_model.js'), 'utf8'));
(0, eval)(fs.readFileSync(process.argv[3].replace('core/escape_html.js', 'ui/translation_indicator.js'), 'utf8'));

// streaming_render.js + chat_render.js CONCATENATED into one script, exactly as
// lib/js_bundler.py emits them — see the module docstring for why.
const streamSrc = fs.readFileSync(process.argv[3].replace('core/escape_html.js', 'ui/streaming_render.js'), 'utf8');
const api = (0, eval)(
  streamSrc + '\n;\n' + chatSrc + '\n;({renderChat, _loadOlderMessages});');
const renderChat = api.renderChat;
const _loadOlderMessages = api._loadOlderMessages;
if (typeof renderChat !== 'function' || typeof _loadOlderMessages !== 'function') {
  console.log('FAIL fns_exposed'); console.log(out.join('\n')); process.exit(0);
}
check('fns_exposed', true);

function mkMsg(id, role, text) {
  return { role: role || 'assistant', _msgId: id, content: text || ('body ' + id) };
}
function layout() {
  const inner = win.document.getElementById('chatInner');
  return Array.from(inner.children).map(el =>
    el.id === '_lazyLoadSentinel' ? 'SENTINEL'
      : el.id === '_lazyLoadSentinelBottom' ? 'SENT_BOT'
      : (el.getAttribute('data-msg-id') || el.id));
}

// 24 messages; _INITIAL_RENDER is 20 → the first paint renders m4..m23 plus the
// head sentinel standing in for the 4 older ones.
const msgs = [];
for (let i = 0; i < 24; i++) msgs.push(mkMsg('m' + i, i % 2 ? 'assistant' : 'user', 'body ' + i));
const conv = { id: 'c1', messages: msgs };
win.conversations = global.conversations = [conv];
win.getActiveConv = global.getActiveConv = () => conv;

// ── 1) Conversation open (full render). ──
renderChat(conv, true);
check('seed_sentinel_at_head', layout()[0] === 'SENTINEL', 'layout0=' + layout()[0]);

// ── 2) Background repaints — this is how cost / file-change / compaction data
//       lands on every conversation open. Each one runs the surgical path. ──
for (let k = 0; k < 3; k++) renderChat(conv, false);

// INVARIANT A: the sentinel is layout furniture pinned at the HEAD. If the
// reconcile treats it as a message node it gets pushed to the bottom here.
check('sentinel_stays_at_head', layout()[0] === 'SENTINEL',
  'layout=' + layout().join(',').slice(0, 200));

// ── 3) Reader scrolls up → the IntersectionObserver fires the REAL loader,
//       which splices the older batch in with `sentinel.after(frag)`. ──
_loadOlderMessages();

// INVARIANT B: DOM order == conv.messages order. This is the user-visible fact
// ("first-round user message renders at the bottom").
const L = layout().filter(x => x !== 'SENTINEL' && x !== 'SENT_BOT');
const want = conv.messages.map(m => m._msgId).filter(id => L.indexOf(id) !== -1);
check('dom_order_matches_messages', L.join(',') === want.join(','),
  'dom=' + L.join(',').slice(0, 200));

// Explicit oldest-vs-newest probe — states the reported symptom directly, so a
// failure names the bug rather than just "order differs".
const idxM0 = L.indexOf('m0');
const idxM23 = L.indexOf('m23');
check('oldest_renders_above_newest', idxM0 >= 0 && idxM0 < idxM23,
  'm0@' + idxM0 + ' m23@' + idxM23);

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_lazy_sentinel_anchor_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             CHAT_RENDER,     # argv[2]
             ESCAPE_HTML,     # argv[3]
             SAFE_HTML,       # argv[4]
             ROOT,            # argv[5]
             nc,              # argv[6]
             _FIXED_ANCHOR,   # argv[7]
             _LEGACY_ANCHOR,  # argv[8]
             ],
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


def _lines(output):
    return {ln[5:].split(' ')[0]: ln[:4].strip()
            for ln in output.splitlines() if ln[:4].strip() in ('PASS', 'FAIL')}


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_lazy_sentinel_stays_at_head_and_order_holds():
    """Background repaints must not migrate #_lazyLoadSentinel down, and after a
    real _loadOlderMessages the DOM order must equal conv.messages order."""
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'lazy-sentinel anchor failures:\n' + output
    lines = _lines(output)
    for key in ('seed_sentinel_at_head', 'sentinel_stays_at_head',
                'dom_order_matches_messages', 'oldest_renders_above_newest'):
        assert lines.get(key) == 'PASS', f'{key} not PASS:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_firstchild_anchor_pushes_sentinel_down_and_inverts_order():
    """NEUTER: restore the `inner.firstChild` fallback (pre-fix code). The
    sentinel then migrates to the bottom and _loadOlderMessages splices the
    OLDEST messages below the newest — proving the fix is load-bearing."""
    output = _run('firstchild')
    assert 'FAIL neuter_not_applied' not in output, (
        'the fixed anchor `_headAnchor()` is absent from chat_render.js — the '
        f'fix has not landed (or was renamed without updating this guard):\n{output}')
    lines = _lines(output)
    assert lines.get('sentinel_stays_at_head') == 'FAIL', (
        'sentinel did NOT migrate under the legacy anchor — the guard no longer '
        f'reproduces the bug it protects against:\n{output}')
    assert (lines.get('dom_order_matches_messages') == 'FAIL'
            or lines.get('oldest_renders_above_newest') == 'FAIL'), (
        'message order survived the migrated sentinel — the order invariant is '
        f'not actually protected by this anchor:\n{output}')
