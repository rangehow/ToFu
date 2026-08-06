"""tests/test_frontend_sse_poll_degraded.py — poll mode as a FIRST-CLASS
degraded state (2026-08-06, epic pt_6cb1607e).

WHY (measured incident, 2026-08-06 14:02:25)
--------------------------------------------
The VS Code tunnel killed all four live SSE streams in the same second. The
resume budget burned out inside the ~3s flap window (zero backoff — fixed in
tests/test_frontend_sse_resume_retry.py), and every conv surrendered to
`_pollFallback` — PERMANENTLY. Three defects this suite pins:

  1. FALSE STALL BANNER — the stall watch (ui/stall_watch.js) is fed ONLY by
     SSE frames, but heartbeat self-ticks only ever EXIST on the SSE lane, so
     the moment a conv surrendered to poll, "not even heartbeat frames
     arrived" became structurally true and the 「已停滞 · 疑似卡死」 banner
     fired 300s later against a HEALTHY, actively-polling turn (the Stop
     affordance nearly killed a live 60-round task: server-side it kept
     producing past 15:10). Poll mode must clear the watch at entry; poll
     responses ARE the liveness proof there.
  2. NO SELF-HEAL — the surrendered conv polled full snapshots (~968KB every
     ~2s) for the rest of the turn, even after the tunnel healed seconds
     later. Poll mode now re-probes the SSE lane every
     `_SSE_REPROBE_INTERVAL_MS` (30s): CURSOR-LESS (poll snapshots overwrite
     content wholesale, so a stale-cursor replay would double the text — the
     probe takes the backend-folded `state` snapshot, the page-reload shape),
     and on attach `_trySSE` owns the stream to done while poll yields.
  3. GHOST WRITES — the poll loop held its closure `assistantMsg` for the
     rest of the turn; a Phase-2 reload replacing conv.messages mid-poll
     would ghost the ref (merges land on an object nothing renders — the
     frozen-bubble mechanism). The merge now re-resolves by stable `_msgId`
     (then `_taskId`) on every successful poll, mirroring dispatchSSEEvent's
     `_rebindAssistant`.

Harness: bare node (the same pattern as test_frontend_poll_null_resp.py),
evaluating the REAL shipped sse_poll_fallback.js with scripted Api.chat.poll /
_trySSE stubs. Scenarios:
  A — entry clears the watch; interval not elapsed ⇒ ZERO probes; poll
      snapshots paint into the IN-TREE placeholder (identity); done path
      finalizes exactly once.
  B — a wholesale conv.messages replacement between polls (the Phase-2
      reload shape) ⇒ the NEXT poll rebinds by _msgId and writes into the
      NEW in-tree object; the ghost ref freezes.
  C — interval 0 ⇒ a failed probe resumes polling, a later probe that
      attaches hands the turn back (poll yields without finalizing); every
      probe enters CURSOR-LESS and clears the persisted cursor.
Plus source-wiring ratchets so the seams rot with the code.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_frontend_sse_poll_degraded.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
POLL_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'sse_poll_fallback.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
/* NOTE: no 'use strict' here — strict-mode eval gets its own scope and the
 * eval'd `_pollFallback` would NOT leak into module scope (bare-eval harness
 * pattern shared with test_frontend_poll_null_resp.py). */
const fs = require('fs');
const CFG = JSON.parse(process.argv[3]);
global.window = global;
if (CFG.reprobeMs != null) global._SSE_REPROBE_INTERVAL_MS = CFG.reprobeMs;

const calls = { stallWatchClear: [], clearSseCursor: [], trySSE: [],
                twUpdate: 0, twStop: 0, finishStream: 0, save: 0 };
global.stallWatchClear = (t) => { calls.stallWatchClear.push(t); };
global._clearSseCursor = (t) => { calls.clearSseCursor.push(t); };
global.twUpdate = () => { calls.twUpdate++; };
global.twStop = () => { calls.twStop++; };
global.finishStream = () => { calls.finishStream++; };
global.saveConversations = () => { calls.save++; };
global.debugLog = () => {};
global.showToast = () => {};
global._checkServerHealth = async () => true;
global._reportClientError = () => {};
global.setStreamPhase = () => {};
global.projectColdSnapshot = (s) => ({ content: s.content || '', thinking: s.thinking || '',
                                      toolRounds: s.toolRounds || [] });
global._resolveAssistantById = (conv, id, fb) => {
  for (let i = conv.messages.length - 1; i >= 0; i--) {
    const m = conv.messages[i];
    if (m && m._msgId === id) return m;
  }
  return fb;
};
global._resolveAssistantByTaskId = (conv, tid) => {
  for (let i = conv.messages.length - 1; i >= 0; i--) {
    const m = conv.messages[i];
    if (m && m.role === 'assistant' && m._taskId === tid) return m;
  }
  return null;
};

const conv = { id: 'conv-dg', activeTaskId: 'task-dg-1', messages: [
  { role: 'user', content: 'q', _msgId: 'm-user' },
  { role: 'assistant', content: '', thinking: '', toolRounds: [],
    _msgId: 'm-ph', _taskId: 'task-dg-1' },
] };
global.conversations = [conv];
global.activeConvId = 'conv-dg';
const placeholder = conv.messages[1];   // the connectToTask recovery placeholder

let pi = 0;
global.Api = { chat: { poll: async () => {
  const step = CFG.polls[Math.min(pi, CFG.polls.length - 1)];
  pi++;
  if (step.swapMessages) {
    /* Simulate a Phase-2 reload BETWEEN polls: wholesale replace
     * conv.messages with fresh server objects (same _msgId, server-side
     * snapshot content). The closure placeholder is now a GHOST. */
    conv.messages = [
      { role: 'user', content: 'q', _msgId: 'm-user' },
      { role: 'assistant', content: step.swapContent, thinking: '', toolRounds: [],
        _msgId: 'm-ph', _taskId: 'task-dg-1' },
    ];
  }
  return { ok: true, status: 200, headers: { get: () => null },
           json: async () => step.data };
} } };

let ti = 0;
global._trySSE = async (convId, taskId, strm, msg) => {
  calls.trySSE.push({ cursorNull: strm._lastEventId == null });
  const step = (CFG.probes && CFG.probes.length)
    ? CFG.probes[Math.min(ti, CFG.probes.length - 1)] : 'fail';
  ti++;
  return step === 'done';
};

eval(fs.readFileSync(process.argv[2], 'utf8'));   // ui/sse_poll_fallback.js (real)

(async () => {
  /* The stale surrender-time cursor the tunnel left behind. */
  const stream = { controller: { signal: { aborted: false } }, _lastEventId: '8502' };
  await _pollFallback('conv-dg', 'task-dg-1', stream, placeholder);
  const inTree = conv.messages[conv.messages.length - 1];
  process.stdout.write('\n' + JSON.stringify({
    calls,
    pollCount: pi,
    ghostContent: placeholder.content,
    inTreeContent: inTree.content,
    inTreeIsPlaceholder: inTree === placeholder,
  }));
})();
"""


def _run(cfg: dict) -> dict:
    probe = os.path.join(ROOT, 'node_modules', '.tmp_poll_degraded_harness.js')
    os.makedirs(os.path.dirname(probe), exist_ok=True)
    with open(probe, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        p = subprocess.run(['node', probe, POLL_JS, json.dumps(cfg)],
                           capture_output=True, text=True, timeout=60, cwd=ROOT)
    finally:
        os.unlink(probe)
    assert p.returncode == 0, f'node failed: {p.stderr}\n{p.stdout}'
    return json.loads(p.stdout.strip().splitlines()[-1])


_RUN1 = {'data': {'status': 'running', 'content': 'x' * 10, 'thinking': 't1',
                  'toolRounds': [], 'taskId': 'task-dg-1', 'phase': None}}
_RUN2 = {'data': {'status': 'running', 'content': 'y' * 30, 'thinking': 't1t2',
                  'toolRounds': [], 'taskId': 'task-dg-1', 'phase': None}}
_DONE = {'data': {'status': 'done', 'content': 'z' * 40, 'thinking': 't1t2',
                  'toolRounds': [], 'taskId': 'task-dg-1', 'finishReason': 'stop'}}


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_entry_clears_stall_watch_and_poll_paints_in_tree():
    """Scenario A: entering poll mode clears the stall watch (the banner can
    never fire against an actively-polling turn); the reprobe interval keeps
    SSE probes at bay; snapshots paint into the SAME in-tree placeholder
    (identity) and the terminal poll finalizes exactly once."""
    r = _run({'reprobeMs': 1e15, 'polls': [_RUN1, _RUN2, _DONE]})
    assert r['calls']['stallWatchClear'] == ['task-dg-1'], \
        f"poll entry must clear the stall watch for the surrendered task: {r}"
    assert r['calls']['trySSE'] == [], \
        f"no probe before the interval elapses: {r}"
    assert r['inTreeIsPlaceholder'] is True and r['inTreeContent'] == 'z' * 40, \
        f"poll snapshots must paint into the in-tree bubble: {r}"
    assert r['calls']['twUpdate'] >= 2, f"the paint seam runs per poll: {r}"
    assert r['calls']['finishStream'] == 1 and r['calls']['twStop'] == 1, \
        f"terminal poll finalizes exactly once: {r}"


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_rebind_after_phase2_replace_writes_in_tree_not_ghost():
    """Scenario B: conv.messages is wholesale-replaced BETWEEN polls (the
    Phase-2 reload shape). The next poll must re-resolve by _msgId and write
    into the NEW in-tree object — the ghost ref must freeze."""
    swap2 = {'swapMessages': True, 'swapContent': 's' * 20,
             'data': {'status': 'running', 'content': 'y' * 30, 'thinking': '',
                      'toolRounds': [], 'taskId': 'task-dg-1', 'phase': None}}
    r = _run({'reprobeMs': 1e15, 'polls': [_RUN1, swap2, _DONE]})
    assert r['ghostContent'] == 'x' * 10, \
        f"the ghost ref must stop receiving merges after the replace: {r}"
    assert r['inTreeIsPlaceholder'] is False, \
        f"after the replace the live object is the server copy: {r}"
    assert r['inTreeContent'] == 'z' * 40, \
        f"post-rebind merges land on the in-tree object: {r}"
    assert r['calls']['finishStream'] == 1, r


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_reprobe_cursorless_and_hands_back_to_sse():
    """Scenario C (interval 0): a failed probe resumes polling; a later probe
    that attaches runs the turn to done and poll YIELDS (no poll-lane
    finalize). Every probe enters cursor-less AND clears the persisted
    cursor — poll snapshots made the stale cursor's incremental replay
    unsafe (it would double the text)."""
    r = _run({'reprobeMs': 0, 'probes': ['fail', 'done'],
              'polls': [_RUN1, _RUN2, _DONE]})
    assert len(r['calls']['trySSE']) == 2, f"failed probe ⇒ keep polling, later probe re-fires: {r}"
    assert all(c['cursorNull'] for c in r['calls']['trySSE']), \
        f"every re-probe must connect CURSOR-LESS (snapshot sync, not delta replay): {r}"
    assert r['calls']['clearSseCursor'] == ['task-dg-1', 'task-dg-1'], \
        f"the persisted cursor must be cleared per probe: {r}"
    assert r['pollCount'] == 1, \
        f"the successful probe hands back immediately — no further polls: {r}"
    assert r['calls']['finishStream'] == 0 and r['calls']['twStop'] == 0, \
        f"on handback _trySSE owns finalization; the poll lane must not double-finalize: {r}"


def test_source_wires_degraded_mode_seams():
    """Source-scan ratchets: strip a seam and the behavioural harnesses above
    could rot green. Pin the four load-bearing lines."""
    src = open(POLL_JS, encoding='utf-8').read()
    assert 'stallWatchClear(taskId)' in src, \
        'poll entry no longer clears the stall watch — the false 「已停滞」 banner returns'
    assert 'const _SSE_REPROBE_INTERVAL_MS' in src, \
        'reprobe cadence const removed — degraded mode is permanent again'
    assert '_lastSseReprobe = Date.now();' in src, \
        'reprobe clock removed'
    assert '_clearSseCursor(taskId)' in src and 'stream._lastEventId = null;' in src, \
        'cursor-less probe discipline removed — stale-cursor replay doubles poll-snapshot text'
    assert src.count('await _trySSE(convId, taskId, stream, assistantMsg)') >= 1, \
        'the SSE re-probe call was removed from the poll loop'
    assert '_resolveAssistantById' in src and '_resolveAssistantByTaskId' in src, \
        'the stable-id rebind was removed — ghost writes after a Phase-2 replace return'


if __name__ == '__main__':
    test_entry_clears_stall_watch_and_poll_paints_in_tree()
    print('PASS test_entry_clears_stall_watch_and_poll_paints_in_tree')
    test_rebind_after_phase2_replace_writes_in_tree_not_ghost()
    print('PASS test_rebind_after_phase2_replace_writes_in_tree_not_ghost')
    test_reprobe_cursorless_and_hands_back_to_sse()
    print('PASS test_reprobe_cursorless_and_hands_back_to_sse')
    test_source_wires_degraded_mode_seams()
    print('PASS test_source_wires_degraded_mode_seams')
    print('ALL GREEN')
