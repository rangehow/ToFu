"""tests/test_tool_settle_all_lanes.py — pt_ac380e3dde2c4c69.

THE GAP THE FIRST BATCH LEFT
----------------------------
pt_67ffc2b7 removed the round-level barrier for tools that go through a
DISPATCH lane (parallel pool, serial write, long-blocking). It wired
``_settle_tool_result`` into 5 call sites. But ``execute_tool_pipeline``'s
pre-phase has SIX ``continue`` branches that never reach any of them — they
record a result and jump straight to the next tool call, so their
``tool_complete`` is still emitted in the post-phase, i.e. AFTER
``pool.shutdown(wait=True)``.

An AST enumeration of every ``continue`` in ``execute_tool_pipeline`` finds 8
(the original ticket named 3 — hence this suite enumerates rather than trusting
a list):

  L116  parse_err / hallucinated-tool rejection   → NOT settled  ✗
  L224  dedup / prefetch cache HIT                → NOT settled  ✗
  L250  write-approval REJECTED                   → NOT settled  ✗
  L261  abort short-circuit                       → NOT settled  ✗
  L350  pre-hook BLOCK                            → NOT settled  ✗
  L370  serial-write abort skip                   → NOT settled  ✗
  L291  long-blocking serial dispatch             → settled inline ✓
  L630  screenshot / no-vision model              → emits its own ✓

Every one of the six is a ZERO-COST path: the tool did not run at all. So they
are precisely the calls that should light up instantly — and instead they are
the ones that wait longest. The sharpest case is the streaming-prefetch cache
hit: ``StreamingToolExecutor`` exists to run a tool WHILE the model is still
emitting tokens (``inject_into_cache`` is called from
``orchestrator/_run.py``), so by dispatch time its result is already in hand.
Measured event order for a prefetched ``read_files`` beside a 1.2s
``web_search``::

    tool_result(cached) → tool_result(slow) → tool_complete(slow) → tool_complete(cached)

The tool that cost nothing settles LAST.

★ THE SECOND, MORE DANGEROUS HALF
---------------------------------
Wiring settle into the abort / reject lanes naively would introduce a defect
strictly worse than the latency. ``stream_reducer.js``'s tool_complete case
reads::

    if (r.status !== 'rejected') r.status = 'done';

It protects ONE terminal verdict. ``aborted`` / ``error`` / ``unanswerable``
are all real round statuses in this codebase (measured: 9 / 15 / 1 assignment
sites), and every one of them would be overwritten to ``done`` by a
tool_complete arriving afterwards — painting a REFUSED or INTERRUPTED tool as
successfully completed. A user could not tell a rejected write from an applied
one.

So the contract is two-sided:
  * a settled round must announce promptly (latency), AND
  * a terminal verdict must never be overwritten by a later completion frame
    (correctness). The second is non-negotiable and is guarded in BOTH
    directions here.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
        tests/test_tool_settle_all_lanes.py -v
"""

from __future__ import annotations

import ast
import inspect
import os
import shutil
import subprocess
import textwrap
import threading
import time

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ═══════════════════════════════════════════════════════════════════
#  Harness — the REAL pipeline, real round constructor, fake tool bodies
# ═══════════════════════════════════════════════════════════════════

def _mk_task(**over):
    t = {
        'id': 'lanes-task-1',
        'convId': 'cv-lanes-1',
        'status': 'running',
        'aborted': False,
        'model': 'test-model',
        'events': [],
        'events_lock': threading.Lock(),
        '_dispatch_heartbeat': 0.0,
        '_t_last_event': 0.0,
        '_attended': False,
    }
    t.update(over)
    return t


def _mk_tc(tc_id: str, fn_name: str, seq: int, *, parse_err=None, args=None):
    """Build a parsed_tcs 7-tuple through the REAL round constructor.

    Hand-rolling the round dict would omit ``tStart`` (stamped by
    ``_build_tool_round_entry``), making every measured duration read 0ms — a
    FIXTURE defect that mimics a product defect. The first batch of this epic
    was bitten by exactly that, so the constructor is mandatory here.
    """
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    _n, round_entry, _ev = _build_tool_round_entry(
        fn_name, args or {}, tc_id, '{}', seq, False)
    tc = {'id': tc_id, 'type': 'function',
          'function': {'name': fn_name, 'arguments': '{}'}}
    return (tc, fn_name, tc_id, dict(args or {}), round_entry['roundNum'],
            round_entry, parse_err)


class _Recorder:
    def __init__(self):
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, task, event):
        with self._lock:
            self.events.append(dict(event))

    def idx(self, tc_id: str, etype: str) -> int:
        for i, e in enumerate(self.events):
            if e.get('toolCallId') == tc_id and e.get('type') == etype:
                return i
        return -1

    def find(self, tc_id: str, etype: str):
        for e in self.events:
            if e.get('toolCallId') == tc_id and e.get('type') == etype:
                return e
        return None

    def types_for(self, tc_id: str):
        return [e['type'] for e in self.events if e.get('toolCallId') == tc_id]

    def ordered(self):
        return [(e.get('type'), e.get('toolCallId')) for e in self.events]


@pytest.fixture()
def rec(monkeypatch):
    r = _Recorder()
    from lib.tasks_pkg import tool_dispatch as facade
    from lib.tasks_pkg.executor import _finalize as exec_finalize
    from lib.tasks_pkg.tool_dispatch import _pipeline
    monkeypatch.setattr(_pipeline, 'append_event', r, raising=False)
    monkeypatch.setattr(facade, 'append_event', r, raising=False)
    monkeypatch.setattr(exec_finalize, 'append_event', r, raising=False)
    return r


@pytest.fixture()
def slow_tools(monkeypatch):
    """A scripted executor: {fn_name: (sleep_s, body)}."""
    script: dict[str, tuple[float, str]] = {}

    def _fake(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
              cfg, project_path, project_enabled, all_tools=None):
        sleep_s, text = script.get(fn_name, (0.0, 'ok'))
        if sleep_s:
            time.sleep(sleep_s)
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        _finalize_tool_round(
            task, rn, round_entry,
            [{'toolName': fn_name, 'title': fn_name, 'snippet': text[:60],
              'source': 'Test', 'fetched': True, 'fetchedChars': len(text)}])
        return tc_id, text, False

    from lib.tasks_pkg.tool_dispatch import _heartbeat, _pipeline
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', _fake, raising=False)
    monkeypatch.setattr(_pipeline, '_execute_tool_one', _fake, raising=False)
    return script


def _run(task, tcs, cfg=None, messages=None):
    from lib.tasks_pkg.tool_dispatch import execute_tool_pipeline
    messages = messages if messages is not None else []
    execute_tool_pipeline(
        task, tcs, cfg=cfg or {'autoApply': True}, project_path=None,
        project_enabled=False, tool_list=[], messages=messages,
        all_search_results_text=[], round_num=0, model='test-model')
    return messages


def _assert_settles_before_slow(rec, fast_id, slow_id, lane):
    """The shared shape: a zero-cost lane must fully settle before the slow
    sibling even produces its result."""
    fast_complete = rec.idx(fast_id, 'tool_complete')
    slow_result = rec.idx(slow_id, 'tool_result')
    assert fast_complete >= 0, (
        '%s lane emitted no tool_complete at all; events for %s: %r'
        % (lane, fast_id, rec.types_for(fast_id)))
    assert slow_result >= 0, 'the slow sibling must produce a result'
    assert fast_complete < slow_result, (
        '%s: this lane costs ZERO time (the tool never ran / was already '
        'cached), yet its tool_complete (idx=%d) lands AFTER the slow '
        "sibling's result (idx=%d) — it is still being settled in the "
        'post-phase behind pool.shutdown(wait=True). Wire the settle into '
        'this lane. Stream: %r'
        % (lane, fast_complete, slow_result, rec.ordered()))


# ═══════════════════════════════════════════════════════════════════
#  Face 0 — enumerate the lanes (do not trust a hand-written list)
# ═══════════════════════════════════════════════════════════════════

def test_every_skip_lane_is_accounted_for():
    """★ Enumerate, don't enumerate-by-hand.

    The originating ticket named 3 skip lanes; an AST walk finds 8. A guard
    that checks only the named ones would go green while an unnamed lane keeps
    the defect — the same "the ticket itself may be incomplete" failure this
    project has hit before.

    This test pins the COUNT and requires each ``continue`` to sit in a branch
    that is either (a) settled, or (b) explicitly listed below as
    deliberately-not-settled with a reason. A new ``continue`` appearing in
    this function fails the test until someone classifies it.
    """
    from lib.tasks_pkg.tool_dispatch import _pipeline

    src = inspect.getsource(_pipeline.execute_tool_pipeline)
    tree = ast.parse(textwrap.dedent(src))
    fn = tree.body[0]

    parent = {}
    for p in ast.walk(fn):
        for c in ast.iter_child_nodes(p):
            parent[c] = p

    lanes = []
    for cont in [n for n in ast.walk(fn) if isinstance(n, ast.Continue)]:
        node, guard = cont, ''
        while node in parent:
            node = parent[node]
            if isinstance(node, ast.If):
                guard = ast.unparse(node.test)
                break
        lanes.append(guard)

    assert len(lanes) == 8, (
        'the skip-lane inventory changed (found %d `continue` branches, '
        'expected 8). Classify the new one: does it settle its round, or does '
        'it have a documented reason not to?\nGuards: %r' % (len(lanes), lanes))

    # Each lane must be recognisable. Keyed on a distinctive token of its guard
    # so a refactor that renames a local does not silently drop a lane.
    expected_tokens = [
        '_parse_err',                 # hallucinated / unparseable tool call
        'cached is not None',         # dedup + streaming-prefetch HIT
        'not approved',               # write-approval REJECTED
        "aborted",                    # abort short-circuit (x2: pre + serial)
        '_serial_cfg',                # long-blocking serial dispatch
        'action ==',                  # pre-hook BLOCK
        'model_supports_vision',      # screenshot on a text-only model
    ]
    joined = ' | '.join(lanes)
    for tok in expected_tokens:
        assert tok in joined, (
            'skip lane matching %r vanished from execute_tool_pipeline — if it '
            'was intentionally removed, update this inventory; if it was '
            'renamed, the settle wiring for it needs re-checking.\nGuards: %r'
            % (tok, lanes))


# ═══════════════════════════════════════════════════════════════════
#  Face 1 — the streaming-prefetch cache hit (the sharpest case)
# ═══════════════════════════════════════════════════════════════════

def test_prefetch_cache_hit_settles_immediately(rec, slow_tools):
    """★ THE LOAD-BEARING FACE.

    ``StreamingToolExecutor`` runs a tool WHILE the model is still streaming
    tokens and injects the result into ``task['_tool_result_cache']`` with
    source='prefetch'. By the time dispatch runs, the answer is already in
    hand — cost ZERO. Yet its tool_complete is currently emitted in the
    post-phase, so the fastest possible tool in the product is the one that
    waits longest.
    """
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

    slow_tools['web_search'] = (1.2, 'SLOW BODY')

    task = _mk_task()
    task['_tool_result_cache'] = {
        _make_cache_key('read_files', {}):
            ('PREFETCHED BODY', False, 'prefetch', None, None, None),
    }
    tcs = [_mk_tc('tc-pf', 'read_files', 1),
           _mk_tc('tc-slow', 'web_search', 2)]
    _run(task, tcs)

    _assert_settles_before_slow(rec, 'tc-pf', 'tc-slow', 'streaming-prefetch hit')

    ev = rec.find('tc-pf', 'tool_complete')
    assert ev.get('toolContent') == 'PREFETCHED BODY', (
        'the prefetched content must reach the UI verbatim; got %r'
        % (ev.get('toolContent'),))


def test_dedup_cache_hit_settles_immediately(rec, slow_tools):
    """Same lane, ``source='dedup'`` — a repeat call inside one turn.

    Also zero-cost (the result is replayed, not recomputed), so it has the
    same obligation.
    """
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

    slow_tools['web_search'] = (1.2, 'SLOW BODY')

    task = _mk_task()
    task['_tool_result_cache'] = {
        _make_cache_key('grep_search', {}):
            ('DEDUP BODY', False, 'dedup', None, None, None),
    }
    tcs = [_mk_tc('tc-dd', 'grep_search', 1),
           _mk_tc('tc-slow', 'web_search', 2)]
    _run(task, tcs)

    _assert_settles_before_slow(rec, 'tc-dd', 'tc-slow', 'dedup hit')


# ═══════════════════════════════════════════════════════════════════
#  Face 2 — the refusal / interruption lanes
# ═══════════════════════════════════════════════════════════════════

def test_parse_error_lane_settles_immediately(rec, slow_tools):
    """A hallucinated or unparseable tool call never executes, so the round is
    knowably finished the instant it is inspected."""
    slow_tools['web_search'] = (1.2, 'SLOW BODY')

    task = _mk_task()
    tcs = [_mk_tc('tc-bad', 'read_files', 1,
                  parse_err='Error: malformed arguments'),
           _mk_tc('tc-slow', 'web_search', 2)]
    _run(task, tcs)

    _assert_settles_before_slow(rec, 'tc-bad', 'tc-slow', 'parse-error')


def test_pre_hook_block_lane_settles_immediately(rec, slow_tools, monkeypatch):
    """A pre-hook block refuses the tool before execution."""
    from lib.tasks_pkg.tool_dispatch import _pipeline

    class _Blocked:
        action = 'block'
        message = 'blocked by policy'
        additional_context = 'try a narrower path'

    def _pre(fn_name, fn_args, task):
        return _Blocked() if fn_name == 'write_file' else None

    monkeypatch.setattr(_pipeline, 'run_pre_hooks', _pre, raising=False)
    slow_tools['web_search'] = (1.2, 'SLOW BODY')

    task = _mk_task()
    tcs = [_mk_tc('tc-blk', 'write_file', 1),
           _mk_tc('tc-slow', 'web_search', 2)]
    _run(task, tcs)

    _assert_settles_before_slow(rec, 'tc-blk', 'tc-slow', 'pre-hook block')


def test_approval_rejected_lane_settles_immediately(rec, slow_tools, monkeypatch):
    """A user who clicks Reject has ALREADY answered — the round is settled at
    that instant, and must not wait for an unrelated slow sibling."""
    from lib.tasks_pkg.tool_dispatch import _pipeline

    monkeypatch.setattr(
        _pipeline, '_handle_approval',
        lambda *a, **k: (False, 'User rejected this write.'), raising=False)
    slow_tools['web_search'] = (1.2, 'SLOW BODY')

    task = _mk_task(_attended=True)
    tcs = [_mk_tc('tc-rej', 'write_file', 1),
           _mk_tc('tc-slow', 'web_search', 2)]
    _run(task, tcs, cfg={'autoApply': False})

    _assert_settles_before_slow(rec, 'tc-rej', 'tc-slow', 'approval rejected')


# ═══════════════════════════════════════════════════════════════════
#  Face 3 — ★ the terminal verdict must SURVIVE the settle
# ═══════════════════════════════════════════════════════════════════

def test_rejected_round_is_never_marked_done(rec, slow_tools, monkeypatch):
    """★ THE CORRECTNESS HALF — worse than the latency if we get it wrong.

    Settling a refused tool must NOT turn it into a success. A round that was
    rejected has to keep saying so on the wire: if the completion frame
    carried no verdict, the client's tool_complete case would flip it to
    'done' and a write the user explicitly REFUSED would render as applied.
    """
    from lib.tasks_pkg.tool_dispatch import _pipeline

    monkeypatch.setattr(
        _pipeline, '_handle_approval',
        lambda *a, **k: (False, 'User rejected this write.'), raising=False)
    slow_tools['web_search'] = (0.3, 'SLOW')

    task = _mk_task(_attended=True)
    rej = _mk_tc('tc-rej', 'write_file', 1)
    _run(task, [rej, _mk_tc('tc-slow', 'web_search', 2)],
         cfg={'autoApply': False})

    assert rej[5]['status'] == 'rejected', (
        "the round's own status must stay 'rejected' after settling; got %r"
        % (rej[5]['status'],))

    ev = rec.find('tc-rej', 'tool_complete')
    if ev is not None:
        assert ev.get('status') == 'rejected', (
            'a tool_complete for a REJECTED round must carry '
            "status='rejected'. Without it the client flips the round to "
            "'done' and a refused write renders as applied — strictly worse "
            'than the latency this epic removes. Event: %r' % (ev,))


def test_aborted_round_is_never_marked_done(rec, slow_tools):
    """Same contract for a user-pressed Stop.

    The abort short-circuit records 'Task aborted by user.' and skips
    execution. Settling that lane must stamp a terminal ABORTED verdict — not
    leave the round dangling for the end-of-task sweep, and never let it read
    'done'.
    """
    task = _mk_task(aborted=True)
    a = _mk_tc('tc-abort', 'read_files', 1)
    _run(task, [a])

    assert a[5].get('status') in ('aborted', 'rejected'), (
        "an aborted tool's round must carry a terminal ABORTED verdict at the "
        'moment it is skipped — relying on the end-of-task dangling sweep '
        'leaves a spinner running for the rest of the turn; got %r'
        % (a[5].get('status'),))
    assert a[5].get('status') != 'done', (
        'an aborted tool must NEVER read as done')

    ev = rec.find('tc-abort', 'tool_complete')
    if ev is not None:
        assert ev.get('status') != 'done', (
            'a completion frame for an aborted round must not claim done: %r'
            % (ev,))


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_client_never_overwrites_a_terminal_verdict():
    """★ The client half, driving the REAL pure reducer.

    Measured on HEAD: the tool_complete case reads
    ``if (r.status !== 'rejected') r.status = 'done'`` — it protects exactly
    ONE verdict. ``aborted`` / ``error`` / ``unanswerable`` are all real round
    statuses in this codebase, and each would be silently promoted to 'done'
    by a later completion frame.

    A backend-only guard cannot catch this: the server could ship a perfect
    ``status='aborted'`` and the client would still overwrite it.
    """
    harness = r"""
    const fs = require('fs');
    const path = require('path');
    global.window = global;
    const _log = console.log.bind(console);
    global.console = { log: _log, warn: () => {}, error: () => {}, debug: () => {} };
    const out = [];
    function check(n, c, d) { out.push((c ? 'PASS ' : 'FAIL ') + n + (c ? '' : '  :: ' + (d || ''))); }

    (0, eval)(fs.readFileSync(path.join(process.argv[1], 'ui/stream_reducer.js'), 'utf8'));

    // Every terminal verdict must survive a later tool_complete.
    for (const verdict of ['rejected', 'aborted', 'error', 'unanswerable']) {
      let st = emptyStreamState();
      st = reduceStreamState(st, { type: 'tool_start', roundNum: 1,
                                   toolCallId: 'tc-1', toolName: 'write_file',
                                   query: 'w' });
      st.toolRounds[0].status = verdict;
      st = reduceStreamState(st, { type: 'tool_complete', roundNum: 1,
                                   toolCallId: 'tc-1', toolContent: 'X' });
      check('verdict_' + verdict + '_survives',
            st.toolRounds[0].status === verdict,
            'a tool_complete must NOT overwrite the terminal verdict "'
            + verdict + '"; got "' + st.toolRounds[0].status + '" — a refused '
            + 'or interrupted tool would render as successfully completed');
      // toolContent must still be attached (the settle is not a no-op).
      check('verdict_' + verdict + '_keeps_content',
            st.toolRounds[0].toolContent === 'X',
            'settling must still attach the content');
    }

    // An explicit status on the frame is honoured.
    {
      let st = emptyStreamState();
      st = reduceStreamState(st, { type: 'tool_start', roundNum: 2,
                                   toolCallId: 'tc-2', toolName: 'write_file',
                                   query: 'w' });
      st = reduceStreamState(st, { type: 'tool_complete', roundNum: 2,
                                   toolCallId: 'tc-2', toolContent: 'Y',
                                   status: 'aborted' });
      check('frame_status_honoured', st.toolRounds[0].status === 'aborted',
            'a tool_complete carrying status must apply it; got '
            + st.toolRounds[0].status);
    }

    // The ordinary in-flight round still settles to done.
    {
      let st = emptyStreamState();
      st = reduceStreamState(st, { type: 'tool_start', roundNum: 3,
                                   toolCallId: 'tc-3', toolName: 'read_files',
                                   query: 'r' });
      st = reduceStreamState(st, { type: 'tool_complete', roundNum: 3,
                                   toolCallId: 'tc-3', toolContent: 'Z' });
      check('searching_still_becomes_done', st.toolRounds[0].status === 'done',
            'a normal in-flight round must still settle to done; got '
            + st.toolRounds[0].status);
    }

    console.log(out.join('\n'));
    """
    proc = subprocess.run(['node', '-e', harness, JS_DIR],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        'harness crashed (rc=%s)\nstdout:\n%s\nstderr:\n%s'
        % (proc.returncode, proc.stdout, proc.stderr))
    lines = [ln for ln in proc.stdout.strip().splitlines()
             if ln.startswith(('PASS', 'FAIL'))]
    failed = [ln for ln in lines if ln.startswith('FAIL')]
    assert not failed, ('terminal-verdict faces failed:\n  '
                        + '\n  '.join(failed))
    assert len(lines) >= 10, (
        'expected 10 checks, got %d:\n%s' % (len(lines), '\n'.join(lines)))


# ═══════════════════════════════════════════════════════════════════
#  Face 4 — regression: ordering + exactly-once still hold
# ═══════════════════════════════════════════════════════════════════

def test_skip_lanes_emit_exactly_one_complete(rec, slow_tools):
    """Wiring a 6th..11th call site must not double-emit.

    ``_settle_tool_result`` is idempotent per tc_id; if a lane settled early
    AND the post-phase settled it again, per-tool token counts would be
    double-counted in the round accounting.
    """
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

    slow_tools['web_search'] = (0.3, 'SLOW')

    task = _mk_task()
    task['_tool_result_cache'] = {
        _make_cache_key('read_files', {}):
            ('PF', False, 'prefetch', None, None, None),
    }
    tcs = [_mk_tc('tc-pf', 'read_files', 1),
           _mk_tc('tc-bad', 'grep_search', 2, parse_err='bad args'),
           _mk_tc('tc-slow', 'web_search', 3)]
    _run(task, tcs)

    for tc_id in ('tc-pf', 'tc-bad', 'tc-slow'):
        n = sum(1 for e in rec.events
                if e.get('toolCallId') == tc_id
                and e.get('type') == 'tool_complete')
        assert n == 1, (
            '%s emitted %d tool_complete events (expected exactly 1); '
            'types=%r' % (tc_id, n, rec.types_for(tc_id)))


def test_message_order_survives_the_new_wiring(rec, slow_tools):
    """The post-phase loop still owns message ORDER.

    Tool messages must enter the list in the model's original tool-call order
    regardless of which lane settled when — an out-of-order
    tool_call/tool_result pair is a hard API error on Anthropic.
    """
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

    slow_tools['web_search'] = (0.5, 'SLOW')

    task = _mk_task()
    task['_tool_result_cache'] = {
        _make_cache_key('read_files', {}):
            ('PF', False, 'prefetch', None, None, None),
    }
    # Declaration order: slow first, then the instant cache hit.
    tcs = [_mk_tc('tc-slow', 'web_search', 1),
           _mk_tc('tc-pf', 'read_files', 2)]
    messages = _run(task, tcs)

    ids = [m['tool_call_id'] for m in messages if m.get('role') == 'tool']
    assert ids == ['tc-slow', 'tc-pf'], (
        'tool messages must follow DECLARATION order even though the cache '
        'hit settled first; got %r' % (ids,))
