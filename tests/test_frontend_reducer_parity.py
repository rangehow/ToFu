#!/usr/bin/env python3
"""RENDER_CONTRACT Phase 3 — GOLDEN parity: cold projection == live projection.

TESTS-FIRST: RED on HEAD by design (the reducer does not exist yet).

The acceptance anchor for Phase 3. Today FIVE independent assemblers hand-mutate
the same in-memory shape (assistantMsg.content/.thinking/.toolRounds) with
different write disciplines (LIVE `+=` append, COLD verbatim `=` +
_snapshotLongerRounds, POLL keep-longer, DONE verbatim, VU render). So the SAME
logical turn can project differently depending on which path delivered it —
tool-round jitter + cold-reopen twinning.

Phase 3 introduces ONE pure reducer, ``static/js/ui/stream_reducer.js``:

    projectStreamEvents(events)        -> {content, thinking, toolRounds}   // LIVE/WARM fold
    projectColdSnapshot(snapshot)      -> {content, thinking, toolRounds}   // COLD state event
    reduceStreamState(state, event)    -> newState                          // the primitive
    locateRound(state, event)          -> round|null                        // the ONE index normalizer

This test builds ONE logical turn as (a) the ordered LIVE event stream and
(b) the COLD ``state`` snapshot the server would emit for that same settled
turn, projects BOTH through the reducer, and asserts the two results are
BYTE-IDENTICAL (JSON.stringify equal). RED now — the module is absent, so the
harness reports ``reducer_missing`` and the test fails. GREEN once the reducer
lands and both paths fold through it (docs/RENDER_CONTRACT_PHASE3_PLAN.md §7
steps 2–3).

Extraction-and-eval in node, mirroring tests/test_frontend_conn_transient_reconnect.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
REDUCER_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'stream_reducer.js')


def _run_node(body: str) -> dict:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')
    with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False) as f:
        f.write(body)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=25)
        # A syntax/absence failure is itself a RED signal, surfaced cleanly.
        if out.returncode != 0:
            return {'ok': False, 'reason': 'node_error', 'stderr': out.stderr[-800:]}
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(tmp)


# One logical turn: prose delta, a tool round (start→result→done), more prose.
# The COLD snapshot is the settled shape the server emits for the SAME turn.
_FIXTURE = r'''
const LIVE_EVENTS = [
  { type: 'delta', content: 'Let me check' },
  { type: 'delta', thinking: 'thinking a bit' },
  { type: 'tool_start', roundNum: 1, toolName: 'run_command', toolCallId: 'tc1', query: 'ls' },
  { type: 'tool_result', roundNum: 1, toolCallId: 'tc1', results: [{toolName:'run_command',title:'ok',snippet:'a.py'}] },
  { type: 'tool_done', roundNum: 1, toolCallId: 'tc1', content: 'a.py\nb.py', isError: false },
  { type: 'delta', content: ' the logs.' },
];
// COLD state snapshot: the server's settled record of that same turn.
const COLD_SNAPSHOT = {
  content: 'Let me check the logs.',
  thinking: 'thinking a bit',
  toolRounds: [
    { roundNum: 1, toolName: 'run_command', toolCallId: 'tc1', query: 'ls',
      status: 'done', results: [{toolName:'run_command',title:'ok',snippet:'a.py'}],
      toolContent: 'a.py\nb.py' },
  ],
};
'''


def _harness(assert_body: str) -> str:
    # Try to load the reducer module; if absent, emit a clean reducer_missing.
    return f'''
import * as fs from 'node:fs';
const REDUCER_PATH = {json.dumps(REDUCER_JS)};
let R = null;
try {{
  if (fs.existsSync(REDUCER_PATH)) {{
    const src = fs.readFileSync(REDUCER_PATH, 'utf-8');
    // The module is plain window-scope JS (bundled, no exports). Eval it and
    // pull the three public fns off the resulting scope.
    const sandbox = {{}};
    const fn = new Function('exports', src + '\\n;' +
      'exports.projectStreamEvents = (typeof projectStreamEvents!=="undefined")?projectStreamEvents:null;' +
      'exports.projectColdSnapshot = (typeof projectColdSnapshot!=="undefined")?projectColdSnapshot:null;' +
      'exports.reduceStreamState = (typeof reduceStreamState!=="undefined")?reduceStreamState:null;');
    fn(sandbox);
    R = sandbox;
  }}
}} catch (e) {{ R = {{ _err: String(e) }}; }}
{_FIXTURE}
if (!R || !R.projectStreamEvents || !R.projectColdSnapshot) {{
  console.log(JSON.stringify({{ ok:false, reason:'reducer_missing',
    detail:'static/js/ui/stream_reducer.js must export projectStreamEvents + projectColdSnapshot' }}));
}} else {{
{assert_body}
}}
'''


def test_cold_projection_equals_live_projection_byte_identical():
    """The golden anchor: LIVE-fold and COLD-snapshot projections of the SAME
    turn must be byte-identical. RED now (reducer_missing); GREEN when the four
    paths fold through the one reducer."""
    body = _harness('''
  const live = R.projectStreamEvents(LIVE_EVENTS);
  const cold = R.projectColdSnapshot(COLD_SNAPSHOT);
  const liveJson = JSON.stringify(live);
  const coldJson = JSON.stringify(cold);
  console.log(JSON.stringify({
    ok: true,
    equal: liveJson === coldJson,
    live: liveJson,
    cold: coldJson,
  }));
''')
    r = _run_node(body)
    if not r.get('ok'):
        pytest.fail(
            'Phase-3 reducer not present yet (tests-first RED): '
            f"{r.get('reason')} — {r.get('detail') or r.get('stderr','')}. "
            'Land static/js/ui/stream_reducer.js (plan §4) and route LIVE+COLD '
            'through it so this golden parity turns GREEN.')
    assert r['equal'], (
        'PROJECTION DIVERGENCE: cold-replay projection != live-fold projection '
        'for the same logical turn — the exact tool-round jitter / cold-reopen '
        f'twinning Phase 3 must kill.\n  live={r["live"]}\n  cold={r["cold"]}')


def test_reducer_module_exists_and_is_pure():
    """A smaller RED signal that stands alone: the reducer module must exist and
    expose the pure projection API. RED now; documents the deliverable."""
    body = _harness('''
  console.log(JSON.stringify({ ok:true, hasReduce: typeof R.reduceStreamState === 'function' }));
''')
    r = _run_node(body)
    if not r.get('ok'):
        pytest.fail(
            'tests-first RED: static/js/ui/stream_reducer.js absent — '
            'this is the Phase-3 deliverable (plan §4). '
            f"reason={r.get('reason')}")
    assert r['hasReduce'], 'stream_reducer.js must expose reduceStreamState(state, event)'


if __name__ == '__main__':
    for fn in (test_reducer_module_exists_and_is_pure,
               test_cold_projection_equals_live_projection_byte_identical):
        try:
            fn(); print('  PASS', fn.__name__)
        except Exception as e:  # noqa: BLE001
            print('  RED ', fn.__name__, '::', str(e)[:200])
