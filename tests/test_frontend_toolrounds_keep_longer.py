"""Regression: a reconnect `state` snapshot (or a racey `done` committedMessage)
must NOT COLLAPSE an in-progress message's tool-round panel by overwriting the
accumulated rounds with an EMPTY/SHORTER array.

WHY
---
`content`/`thinking` already go through `_snapshotLonger` (keep-the-longer), but
`toolRounds` used to be assigned VERBATIM at all five state-snapshot sites and
in the terminal `done` handler:

    assistantMsg.toolRounds = existing.concat(ev.toolRounds || []);   // state
    assistantMsg.toolRounds = _cm.toolRounds;                         // done

On a flaky network the reconnect snapshot can replay a COLD/empty 5s checkpoint
whose `toolRounds` is `[]` (or shorter than what the client already accumulated
from live deltas) — verbatim assignment then BLANKS the searches / file-edits
panel the user was watching, mid-stream. Same content-regression class as the
2026-07-03 `_snapshotLonger` text fix, just never extended to rounds.

The fix adds `_snapshotLongerRounds(current, incoming)` — a rounds snapshot may
only GROW (adopt incoming only when `incoming.length >= current.length`) —
applied at all five snapshot sites (2 planner, worker, plain-assistant merge)
plus the terminal `done` committedMessage assignment.

Tests (drive the REAL shipped `_snapshotLongerRounds` under node):
  1. empty incoming → keeps the current (longer) rounds. ★ THE FIX.
  2. shorter incoming → keeps current.
  3. longer incoming → adopts it (behaviour preservation).
  4. equal length → adopts incoming (idempotent, >= comparison).
  5. empty current + non-empty incoming → adopts incoming (fresh fill).
  6. null-safe.
  ★ Double-neuter: revert the helper body to `return inc;` (raw adopt) → test #1
    and #2 FAIL (panel collapsed). Plus source guards asserting the six call
    sites route through the helper, not a raw `= ev.toolRounds` / `= _cm.toolRounds`.
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
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
const SSE = process.argv[2];

const src = fs.readFileSync(SSE, 'utf8');
const m = src.match(/function _snapshotLongerRounds\([\s\S]*?\n}/);
if (!m) { console.log('FAIL helper_not_found'); process.exit(0); }
eval(m[0]);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

check('helper_defined', typeof _snapshotLongerRounds === 'function');

const R = n => Array.from({length: n}, (_, i) => ({tool: 'r' + i}));

// 1. Empty incoming must NOT shrink a non-empty rounds array. ★ THE FIX.
{
  const cur = R(3), inc = [];
  check('empty_incoming_keeps_current', _snapshotLongerRounds(cur, inc) === cur);
}
// 2. Shorter incoming must NOT shrink.
{
  const cur = R(4), inc = R(1);
  check('shorter_incoming_keeps_current', _snapshotLongerRounds(cur, inc) === cur);
}
// 3. Longer incoming IS adopted.
{
  const cur = R(1), inc = R(5);
  check('longer_incoming_adopted', _snapshotLongerRounds(cur, inc) === inc);
}
// 4. Equal length → adopt incoming (idempotent).
{
  const cur = R(2), inc = R(2);
  check('equal_len_adopts_incoming', _snapshotLongerRounds(cur, inc) === inc);
}
// 5. Empty current + non-empty incoming → adopt incoming (fresh fill).
{
  const inc = R(2);
  check('empty_current_adopts_incoming', _snapshotLongerRounds([], inc) === inc);
}
// 6. null-safe (returns an array both ways).
{
  check('null_safe',
    Array.isArray(_snapshotLongerRounds(null, null))
    && _snapshotLongerRounds(null, R(2)).length === 2
    && _snapshotLongerRounds(R(2), null).length === 2);
}

console.log(out.join('\n'));
"""


def _run():
    harness = os.path.join(HERE, '_toolrounds_keep_longer_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, 'ui', 'sse_pipeline.js')],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'toolRounds keep-longer failures:\n' + output
    assert output.count('PASS') >= 6, f'expected >=6 PASS lines, got:\n{output}'

    # ── Source guards ──
    with open(os.path.join(JS_DIR, 'ui', 'sse_pipeline.js'), encoding='utf-8') as f:
        sse_src = f.read()

    assert 'function _snapshotLongerRounds' in sse_src, '_snapshotLongerRounds helper removed'
    assert 'inc.length >= cur.length' in sse_src, (
        'fix regression: _snapshotLongerRounds no longer keeps-the-longer — a '
        'shorter/empty snapshot could collapse the tool-round panel.')

    # Five call sites route through the helper (2 planner + worker +
    # plain-assistant merge + done). The endpoint-critic snapshot block has no
    # toolRounds assignment, so it is NOT among them. +1 for the definition.
    occurrences = len(re.findall(r'_snapshotLongerRounds\(', sse_src))
    assert occurrences >= 6, (
        f'expected >=5 _snapshotLongerRounds call sites (+1 def = 6 '
        f'occurrences), found {occurrences} — a toolRounds site may have '
        'regressed to a raw `= ev.toolRounds` / `= _cm.toolRounds` overwrite.')

    # The specific old-shape raw assignments must be gone.
    for bad in ('plannerMsg.toolRounds = ev.toolRounds',
                'workerMsg.toolRounds = ev.toolRounds',
                'assistantMsg.toolRounds = _cm.toolRounds',
                'assistantMsg.toolRounds = existing.concat(ev.toolRounds || [])'):
        assert bad not in sse_src, (
            f'fix regression: raw toolRounds overwrite reintroduced: {bad!r}')


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_toolrounds_keep_longer():
    _run()


if __name__ == '__main__':
    if not _node_available():
        print('SKIP — node not available')
    else:
        _run()
        print('PASS test_toolrounds_keep_longer')
