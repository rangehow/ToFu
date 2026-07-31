"""Acceptance gate for the live run_command countdown (epic pt_1a82ffb3).

THE REQUIREMENT
---------------
A `run_command` that declared a `timeout` must render a real-time countdown,
and — the part that is easy to fake — that countdown must SURVIVE switching
conversations. It must resume from where it was, never restart from zero.

WHY A DB ROUND-TRIP AND NOT A REDUCER UNIT TEST
-----------------------------------------------
"Persisted" is a claim about the DATABASE, so a reducer-only test cannot
falsify it. Three independent holes each break persistence while leaving a
reducer test perfectly green, and each is pinned below:

  1. `tool_progress` had NO branch in the pure reducer, so everything it
     carried lived only on the live path and was absent from
     `projectColdSnapshot` (test_reducer_folds_deadline_from_tool_progress +
     test_cold_projection_preserves_deadline).
  2. Neither periodic checkpoint fires during a long command — the
     orchestrator's runs after a round COMPLETES, the stream's on a content
     delta — so whether a running round reached the DB at all was a race. The
     spawn callback now forces one write (see `_make_run_command_spawn_cb`),
     proven end-to-end by
     test_running_round_with_deadline_survives_a_real_db_roundtrip.
  3. Both checkpoint writers early-returned when content+thinking were empty,
     which is exactly a tool-only turn
     (test_tool_only_turn_is_checkpointed_despite_empty_prose).

Plus the reason the deadline cannot be computed on the client at all:
the effective budget is the requested timeout AFTER the cross-DC multiplier
and the MAX_COMMAND_TIMEOUT clamp
(test_deadline_reflects_effective_budget_not_the_requested_one).

NOTE on scope: `run_command` resolves timeout=None BY DESIGN (no ceiling,
pinned by tests/test_no_backend_timeouts.py), so most commands have NO
deadline. For those the truthful display is the ELAPSED count-up, which is why
the renderer covers both and why no cap was introduced to manufacture a
countdown.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

REDUCER_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'stream_reducer.js')
TOOL_ROUNDS_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'tool_rounds.js')


# ══════════════════════════════════════════════════════════════════
#  1. Backend: the spawn clock publishes the EFFECTIVE budget
# ══════════════════════════════════════════════════════════════════

def test_spawn_publishes_absolute_deadline_for_an_explicit_timeout():
    """on_spawn fires once with (exec_start_ms, deadline_ms) in epoch ms."""
    import lib.project_mod.run_command as rc
    seen = []
    rc.tool_run_command('/tmp', 'echo hi', timeout=7,
                        on_spawn=lambda s, d: seen.append((s, d)))
    assert len(seen) == 1, f'on_spawn must fire exactly once, got {len(seen)}'
    start, deadline = seen[0]
    now = time.time() * 1000.0
    assert abs(start - now) < 60_000, 'exec_start must be epoch MILLISECONDS'
    assert abs((deadline - start) / 1000.0 - 7.0) < 0.5


def test_unbounded_command_publishes_no_deadline():
    """No timeout is the DEFAULT. It must yield None — never 0, never a past
    instant, either of which would render as an instantly-expired countdown."""
    import lib.project_mod.run_command as rc
    seen = []
    rc.tool_run_command('/tmp', 'echo hi', on_spawn=lambda s, d: seen.append(d))
    assert seen == [None]
    seen.clear()
    # timeout=0 is the documented "unlimited" spelling.
    rc.tool_run_command('/tmp', 'echo hi', timeout=0,
                        on_spawn=lambda s, d: seen.append(d))
    assert seen == [None]


def test_deadline_reflects_effective_budget_not_the_requested_one():
    """THE reason the client cannot compute this itself.

    A DolphinFS path multiplies the timeout (run_command.py cross-DC block). A
    frontend that read `toolArgs.timeout` would count to zero while the command
    was still legitimately running.
    """
    import lib.project_mod.run_command as rc
    seen = []
    rc.tool_run_command('/tmp', 'echo hi', timeout=10,
                        on_spawn=lambda s, d: seen.append((s, d)))
    base_budget = (seen[0][1] - seen[0][0]) / 1000.0

    seen.clear()
    import lib.cross_dc as _cross_dc
    _orig_mult = _cross_dc.get_timeout_multiplier
    _cross_dc.get_timeout_multiplier = lambda p: 3.0
    try:
        rc.tool_run_command('/tmp', 'echo hi', timeout=10,
                            on_spawn=lambda s, d: seen.append((s, d)))
    finally:
        _cross_dc.get_timeout_multiplier = _orig_mult
    scaled_budget = (seen[0][1] - seen[0][0]) / 1000.0

    assert abs(base_budget - 10.0) < 0.5
    assert abs(scaled_budget - 30.0) < 0.5, (
        f'cross-DC multiplier must be baked into the published deadline '
        f'(got {scaled_budget:.1f}s, expected ~30s). A client deriving the '
        f'countdown from the REQUESTED timeout would hit zero 20s early.')


def test_a_raising_spawn_callback_never_breaks_the_command():
    """Telemetry must never abort a running command."""
    import lib.project_mod.run_command as rc

    def _boom(_s, _d):
        raise RuntimeError('spawn callback exploded')

    out = rc.tool_run_command('/tmp', 'echo survived', on_spawn=_boom)
    assert 'survived' in out and 'exit code: 0' in out


# ══════════════════════════════════════════════════════════════════
#  2. Checkpoint: a tool-only running round reaches the DB
# ══════════════════════════════════════════════════════════════════

def test_tool_only_turn_is_checkpointed_despite_empty_prose():
    """Both checkpoint writers used to early-return on empty content+thinking.

    A turn whose first act is a long `run_command` has empty content AND empty
    thinking for its entire duration, so that guard dropped precisely the write
    that makes the running round recoverable.
    """
    from lib.tasks_pkg.manager._sync import _has_inflight_round

    running = {'toolRounds': [{'roundNum': 1, 'status': 'searching',
                               'deadlineTs': time.time() * 1000 + 60_000}]}
    assert _has_inflight_round(running) is True

    # A settled round is NOT in flight — it must not keep re-arming writes.
    assert _has_inflight_round(
        {'toolRounds': [{'roundNum': 1, 'status': 'done'}]}) is False
    assert _has_inflight_round({'toolRounds': []}) is False

    # A command blocked on a human gate is emphatically still running — this is
    # the longest-lived case and the one most likely to span a conv switch.
    for st in ('executing', 'pending_approval', 'awaiting_human', 'awaiting_stdin'):
        assert _has_inflight_round({'toolRounds': [{'status': st}]}) is True, st


def test_has_real_round_still_means_settled():
    """Guard the guard. The in-flight predicate must NOT be implemented by
    loosening `has_real_round`: the ghost sweep and the tail classifier rely on
    it meaning SETTLED, and widening it would make an unsettled bodyless bubble
    un-sweepable."""
    from lib.conversations.reconcile import has_real_round
    assert has_real_round({'toolRounds': [{'status': 'searching'}]}) is False
    assert has_real_round({'toolRounds': [{'status': 'done'}]}) is True


# ══════════════════════════════════════════════════════════════════
#  3. THE ACCEPTANCE TEST — real DB round-trip
# ══════════════════════════════════════════════════════════════════

def test_running_round_with_deadline_survives_a_real_db_roundtrip():
    """Write a RUNNING round carrying tStart+deadlineTs through the REAL
    checkpoint writer into a REAL sqlite conversation row, then read it back
    the way a conversation switch does.

    This is the acceptance criterion: after the round-trip the deadline must
    still be there and still be in the FUTURE, so the countdown resumes
    mid-flight instead of restarting from zero.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    import lib.tasks_pkg.manager as mgr
    from lib.tasks_pkg.manager import (
        _conv_latest_task, _conv_latest_task_lock, create_task)

    db = get_thread_db(DOMAIN_CHAT)
    conv_id = 'deadline-acceptance'
    now_ms = int(time.time() * 1000)
    deadline_ms = now_ms + 300_000        # 5 minutes out
    exec_start_ms = now_ms - 42_000       # already 42s in

    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'deadline-acceptance',
        'messages': json_dumps_pg([
            {'role': 'user', 'content': 'run the build'},
            {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': []},
        ]),
        'msg_count': 2, 'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()

    task = create_task(conv_id, [{'role': 'user', 'content': 'run the build'}], {})
    task['content'] = ''          # ← tool-only turn: no prose at all
    task['thinking'] = ''
    task['toolRounds'] = [{
        'roundNum': 1,
        'toolName': 'run_command',
        'toolCallId': 'tc_build_1',
        'query': 'make -j8',
        'status': 'searching',
        'results': None,
        'tStart': exec_start_ms - 5_000,
        'execStartTs': exec_start_ms,
        'deadlineTs': deadline_ms,
    }]
    with _conv_latest_task_lock:
        _conv_latest_task[conv_id] = task['id']

    try:
        mgr.checkpoint_task_partial(task, force=True)

        # ── Read back exactly as a conversation switch would ──
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)).fetchone()
        assert row and row[0], 'conversation row vanished'
        tail = json.loads(row[0])[-1]
        assert tail.get('role') == 'assistant'

        rounds = tail.get('toolRounds') or []
        assert rounds, (
            'THE BUG: the running round did not reach the DB at all, so '
            'switching conversations mid-command finds nothing to project and '
            'the countdown restarts from zero.')

        r = rounds[0]
        assert r.get('status') == 'searching', r.get('status')
        assert r.get('deadlineTs') == deadline_ms, (
            f'deadlineTs lost in persistence: {r.get("deadlineTs")!r}')
        assert r.get('execStartTs') == exec_start_ms, (
            f'execStartTs lost in persistence: {r.get("execStartTs")!r}')
        assert r['deadlineTs'] > time.time() * 1000, (
            'the recovered deadline is already in the past — the countdown '
            'would render "terminating…" for a healthy running command')

        remaining_s = (r['deadlineTs'] - time.time() * 1000) / 1000.0
        assert 240 < remaining_s < 300, (
            f'recovered countdown should resume near ~258s, got {remaining_s:.0f}s')
    finally:
        from lib.database import db_execute_with_retry
        with _conv_latest_task_lock:
            _conv_latest_task.pop(conv_id, None)
        db_execute_with_retry(
            db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db_execute_with_retry(
            db, 'DELETE FROM task_results WHERE task_id=?', (task['id'],))
        db.commit()


# ══════════════════════════════════════════════════════════════════
#  4. The pure reducer folds tool_progress (live AND cold)
# ══════════════════════════════════════════════════════════════════

def _node(script: str) -> dict:
    if shutil.which('node') is None:
        pytest.skip('node is required for the reducer projection gate')
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as fh:
        fh.write(script)
        path = fh.name
    try:
        proc = subprocess.run(['node', path], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise AssertionError(f'node harness failed:\n{proc.stderr[:2000]}')
        return json.loads(proc.stdout)
    finally:
        os.unlink(path)


_HARNESS_HEAD = f'''
const fs = require('fs');
const src = fs.readFileSync({json.dumps(REDUCER_JS)}, 'utf8');
const mod = {{ exports: {{}} }};
new Function('module', 'exports', src)(mod, mod.exports);
const {{ projectStreamEvents, projectColdSnapshot, reduceStreamState }} = mod.exports;
'''


def test_reducer_folds_deadline_from_tool_progress():
    """HOLE #1: before this, `tool_progress` had no reducer branch at all — so
    the deadline existed only on the live pipeline path."""
    out = _node(_HARNESS_HEAD + '''
const evs = [
  { type:'tool_start', roundNum:1, toolCallId:'tc1', toolName:'run_command',
    query:'make', tStart: 1000 },
  { type:'tool_progress', roundNum:1, toolCallId:'tc1',
    execStartTs: 5000, deadlineTs: 65000, chunk:'compiling...' },
];
const proj = projectStreamEvents(evs);
const r = proj.toolRounds[0] || {};
console.log(JSON.stringify({
  deadline: r.deadlineTs, execStart: r.execStartTs,
  partial: r._partialOutput, status: r.status,
}));
''')
    assert out['deadline'] == 65000, 'reducer dropped deadlineTs'
    assert out['execStart'] == 5000, 'reducer dropped execStartTs'
    assert out['partial'] == 'compiling...'
    assert out['status'] == 'searching', (
        'a progress frame must NOT settle the round — it means "still going"')


def test_cold_projection_preserves_deadline():
    """The conversation-switch path. A cold snapshot from the DB must project
    the deadline, else the countdown restarts from nothing."""
    out = _node(_HARNESS_HEAD + '''
const snap = { content:'', thinking:'', toolRounds: [
  { roundNum:1, toolName:'run_command', toolCallId:'tc1', status:'searching',
    tStart: 1000, execStartTs: 5000, deadlineTs: 65000,
    _partialOutput: 'compiling...' },
]};
const r = (projectColdSnapshot(snap).toolRounds || [])[0] || {};
console.log(JSON.stringify({
  deadline: r.deadlineTs, execStart: r.execStartTs, partial: r._partialOutput,
}));
''')
    assert out['deadline'] == 65000, (
        'THE BUG: projectColdSnapshot dropped deadlineTs — switching '
        'conversations mid-command restarts the countdown')
    assert out['execStart'] == 5000
    assert out['partial'] == 'compiling...'


def test_live_and_cold_reach_the_same_deadline():
    """The parity contract: folding the events and projecting the settled
    snapshot of the SAME turn must agree on every timing field."""
    out = _node(_HARNESS_HEAD + '''
const evs = [
  { type:'tool_start', roundNum:1, toolCallId:'tc1', toolName:'run_command',
    query:'make', tStart: 1000 },
  { type:'tool_progress', roundNum:1, toolCallId:'tc1',
    execStartTs: 5000, deadlineTs: 65000, chunk:'a' },
  { type:'tool_progress', roundNum:1, toolCallId:'tc1', chunk:'b' },
];
const live = projectStreamEvents(evs).toolRounds[0];
const cold = projectColdSnapshot({ content:'', thinking:'', toolRounds:[{
  roundNum:1, toolName:'run_command', toolCallId:'tc1', query:'make',
  status:'searching', results:null, tStart:1000, execStartTs:5000,
  deadlineTs:65000, _partialOutput:'ab', llmRound:null, _swarm:false,
}]}).toolRounds[0];
console.log(JSON.stringify({
  sameDeadline: live.deadlineTs === cold.deadlineTs,
  sameStart: live.execStartTs === cold.execStartTs,
  samePartial: live._partialOutput === cold._partialOutput,
  livePartial: live._partialOutput,
}));
''')
    assert out['livePartial'] == 'ab', (
        f'chunks must APPEND exactly once, got {out["livePartial"]!r} — a '
        f'double-apply means the pipeline kept its own private write path')
    assert out['sameDeadline'] and out['sameStart'] and out['samePartial']


def test_pipeline_has_no_second_private_write_path():
    """The handler must DELEGATE to the reducer. If it kept mutating the round
    itself, every chunk would be applied twice (reducer + handler) — which is
    how the previous shape would have failed after this migration.

    Comments AND string literals are stripped before scanning (charter #24 via
    the shared tests/_source_scan primitive): this file's own explanatory
    comments mention `_partialOutput` by name, and a guard a comment can
    violate is a false alarm that trains people to ignore it.
    """
    from tests._source_scan import js_function_body, strip_comments

    with open(os.path.join(ROOT, 'static', 'js', 'ui', 'sse_handlers_io.js'),
              encoding='utf-8') as fh:
        src = fh.read()
    body = js_function_body(src, '_handleToolProgress')
    live = strip_comments(body, lang='js', strings=True)
    assert 'reduceStreamState' in live, (
        '_handleToolProgress must fold through the pure reducer')
    assert '_partialOutput' not in live, (
        'the handler still writes _partialOutput itself — with the reducer '
        'branch in place that double-appends every chunk')
    for tok in ('_batchTotal', '_batchDone', 'qrImages'):
        assert tok not in live, (
            f'{tok} is still written by the handler; it belongs to the reducer '
            f'so the cold projection carries it too')


# ══════════════════════════════════════════════════════════════════
#  5. Renderer: countdown / count-up / no negative numbers
# ══════════════════════════════════════════════════════════════════

def test_timer_chip_counts_down_up_and_never_goes_negative():
    out = _node(f'''
const fs = require('fs');
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.t = (k, d) => (d || k);
global.Icon = (n, s) => '';
global.renderMarkdown = (s) => s;
global._shortUrl = (u) => u;
global.formatNumber = (n) => String(n);
global.window = {{ addEventListener(){{}}, removeEventListener(){{}} }};
global.document = {{ addEventListener(){{}}, removeEventListener(){{}},
  querySelectorAll: () => [],
  createElement: () => ({{ style:{{}}, setAttribute(){{}}, appendChild(){{}} }}) }};
eval(fs.readFileSync({json.dumps(TOOL_ROUNDS_JS)}, 'utf8'));
if (global.window._cmdTimerTicker) clearInterval(global.window._cmdTimerTicker);
// A REALISTIC epoch. An artificially small NOW makes `NOW - 3900000` NEGATIVE,
// which _cmdTimerAnchor correctly rejects as "not a real clock" — so a toy
// value would test the rejection path instead of the hour formatting.
const NOW = 1785000000000;
const mk = (o) => _cmdTimerState(o, NOW);
console.log(JSON.stringify({{
  countdown:  mk({{ execStartTs: NOW - 5000, deadlineTs: NOW + 90000 }}).txt,
  soonCls:    mk({{ execStartTs: NOW - 5000, deadlineTs: NOW + 5000 }}).cls,
  expired:    mk({{ execStartTs: NOW - 5000, deadlineTs: NOW - 30000 }}).txt,
  expiredCls: mk({{ execStartTs: NOW - 5000, deadlineTs: NOW - 30000 }}).cls,
  countUp:    mk({{ execStartTs: NOW - 195000 }}).txt,
  hours:      mk({{ execStartTs: NOW - 3900000 }}).txt,
  prefersExec: mk({{ tStart: NOW - 600000, execStartTs: NOW - 30000 }}).txt,
  noClock:    mk({{}}),
  tickerArmed: typeof _tickCmdTimers === 'function',
}}));
// tool_rounds.js installs a REAL setInterval on load (the 1 Hz chip ticker).
// An unref'd interval keeps node's event loop alive forever, so the harness
// must exit explicitly — the same trap tests/_tool_rounds_wire_parity_harness.js
// documents. clearInterval above only covers the handle we can see via
// window._cmdTimerTicker; exit() is the guarantee.
process.exit(0);
''')
    assert out['countdown'] == '1m30s left'
    assert 'soon' in out['soonCls'], 'under 10s must escalate visually'
    assert 'terminating' in out['expired'], (
        f'past the deadline must say what is happening, not show a negative '
        f'number; got {out["expired"]!r}')
    assert 'over' in out['expiredCls']
    assert out['countUp'] == '3m15s', (
        'a command with NO timeout — the DEFAULT — must still show elapsed time')
    assert out['hours'] == '1h05m'
    assert out['prefersExec'] == '30s', (
        'must anchor on execStartTs (spawn), not tStart (round announce): a '
        'write-approval gate between them would over-report execution by 9.5m')
    assert out['noClock'] is None, 'no clocks at all → render nothing, not "NaN"'
    assert out['tickerArmed'] is True
