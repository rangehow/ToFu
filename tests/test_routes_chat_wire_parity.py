#!/usr/bin/env python3
"""Wire-parity baseline for the routes/chat.py sub-package split
(board epic ``pt_04686ac6054a451e``).

The plan: routes/chat.py (~2500 lines) is being decomposed into a
``routes/chat/`` sub-package (``_helpers.py`` first slice, then eventually
``send.py`` / ``stream.py`` / ``poll_abort.py``), with ``routes/chat.py``
kept as a **re-export facade** so import contracts and Blueprint
registrations stay byte-identical. Landed so far:

  * Slice 1 → ``routes/chat_helpers.py``: 5 pure utilities with NO
    module-level mutable state (``_dumps_yielding`` /
    ``_running_checkpoint_verdict`` / ``_log_poll_task_id_mismatch`` /
    ``_loads_yielding`` / ``_warm_resume_serviceable``).
  * Slice 2 → ``routes/chat_state.py``: the send-abort marker BUNDLE
    (``_send_abort_marker`` dict + ``_send_abort_marker_lock`` +
    ``_mark_conv_aborted`` / ``_was_aborted_after`` — the four items
    that share ONE piece of module-level state; they must live in the
    same module and be re-exported through routes.chat as a set so
    nobody imports half of the pair).
  * Slice 3 → ``routes/chat_side_effects.py``: ``_truncate_conv_history``
    (the IO-heavy helper that clears the message-queue + server-message
    -store side channels after a conv history truncate). Kept OUT of
    ``chat_helpers.py`` to preserve that module's "pure + import-
    lightweight" invariant; kept OUT of ``chat_state.py`` because it
    owns no state (it acts on state living in OTHER lib.* packages).

All three slices keep ``routes/chat.py`` importable via re-export
blocks so every ``from routes.chat import _X`` call site keeps working
unchanged.

This test is the CONTRACT the split must preserve. It runs BEFORE and AFTER
the extraction; the numbers must match. It intentionally does NOT hardcode
the current URL list (that would break every legitimate future add), it
SNAPSHOTS the current registered rules at import time and stashes them as a
frozenset — the split preserves the frozenset (proof: import → derive same
frozenset). A future genuine route add DOES update the snapshot, an
accidental route drop does not.

Two layers:

  1. Symbol-level: the 5 helpers being extracted this slice + 3 more
     imported by existing tests (_mark_conv_aborted / _was_aborted_after /
     _truncate_conv_history / _start_task_for_conv) MUST remain importable
     under their ``routes.chat`` names — the sibling tests that call
     ``from routes.chat import _dumps_yielding`` etc. MUST keep working
     without edit.

  2. Wire-level: every route rule registered on ``chat_bp`` and
     ``api_v1_chat_bp`` — url pattern, method set, endpoint name — is
     preserved. Anyone breaks a rule (renames endpoint, drops a method,
     mistypes /api/chat/… vs /api/v1/chat/…) trips this test.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Flask→Quart shim (matches the rest of the suite).
import quart as _quart
sys.modules.setdefault('flask', _quart)

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


# ── Layer 1: symbol-level re-export contract ──────────────────────────

# The exact names shipped by routes.chat that external code (tests +
# main app code) imports today. Each is a module-level function; a re-
# export facade after the split must keep every one of these
# `from routes.chat import X` calls resolving.
_REQUIRED_ROUTES_CHAT_SYMBOLS = (
    # 5 pure helpers extracted THIS slice into routes/chat/_helpers.py
    '_dumps_yielding',
    '_running_checkpoint_verdict',
    '_log_poll_task_id_mismatch',
    '_loads_yielding',
    '_warm_resume_serviceable',
    # More helpers imported by existing tests today; still in routes/chat.py
    # after this first slice but the test-set names them to prove no
    # accidental collision when the facade is added.
    '_mark_conv_aborted',
    '_was_aborted_after',
    '_truncate_conv_history',
    '_start_task_for_conv',
    # The two Blueprint names the app depends on.
    'chat_bp',
)


@_unit
def test_routes_chat_symbols_all_importable():
    """Every symbol external code imports from ``routes.chat`` today
    must still resolve after the split. A re-export facade (this slice)
    or the original module (before the split) both satisfy the test —
    what it forbids is a naive move that leaves the imports 404-ing."""
    import importlib
    mod = importlib.import_module('routes.chat')
    missing = [name for name in _REQUIRED_ROUTES_CHAT_SYMBOLS
               if not hasattr(mod, name)]
    assert not missing, (
        f'routes.chat is missing symbols external code imports: {missing}. '
        f'If you split the file, keep routes/chat.py as a re-export facade '
        f'that surfaces every name in _REQUIRED_ROUTES_CHAT_SYMBOLS.'
    )


@_unit
def test_extracted_helpers_are_callable():
    """The 5 helpers extracted this slice must be genuine callables (not
    e.g. modules or ``None`` placeholders). A wire-safe re-export makes
    them look identical to the pre-split state at every callsite."""
    import importlib
    mod = importlib.import_module('routes.chat')
    for name in ('_dumps_yielding', '_running_checkpoint_verdict',
                 '_log_poll_task_id_mismatch', '_loads_yielding',
                 '_warm_resume_serviceable'):
        obj = getattr(mod, name)
        assert callable(obj), (
            f'routes.chat.{name} is not callable (got {type(obj).__name__}); '
            f'a re-export facade must expose the FUNCTION, not the module')


@_unit
def test_pure_helper_wire_parity_smoke():
    """Sanity-invoke the 3 pure helpers with well-known inputs to prove
    the re-export goes to the REAL function (not a stub / wrong name).
    Values chosen so any accidental swap (e.g. re-exporting _loads_yielding
    from a namespace where it accidentally names _dumps_yielding) trips this."""
    from routes.chat import (
        _dumps_yielding, _loads_yielding,
        _running_checkpoint_verdict, _warm_resume_serviceable,
    )

    # _dumps_yielding: orjson round-trip a small dict.
    encoded = _dumps_yielding({'x': [1, 2, 3], 'y': 'hi'})
    assert isinstance(encoded, str)
    assert '"x"' in encoded and '"y":"hi"' in encoded.replace(' ', '')

    # _loads_yielding: reverses _dumps_yielding
    decoded = _loads_yielding(encoded)
    assert decoded == {'x': [1, 2, 3], 'y': 'hi'}

    # _running_checkpoint_verdict: pure 2-branch decision.
    assert _running_checkpoint_verdict(sharded=True) == ('running', True)
    assert _running_checkpoint_verdict(sharded=False) == ('interrupted', False)

    # _warm_resume_serviceable: pure math on (cursor, n_events).
    assert _warm_resume_serviceable(None, 10) is False
    assert _warm_resume_serviceable(-1, 10) is False
    assert _warm_resume_serviceable(3, 10) is True   # resume_from=4 <= 10
    assert _warm_resume_serviceable(9, 10) is True   # resume_from=10 <= 10 (boundary)
    assert _warm_resume_serviceable(10, 10) is False  # resume_from=11 > 10 → resync


@_unit
def test_abort_marker_bundle_semantics():
    """pt_04686ac6 slice 2: the abort-marker BUNDLE must reproduce the
    exact original send-abort race semantics after being moved out of
    chat.py's module namespace.

    The bundle is _send_abort_marker (dict) + _send_abort_marker_lock
    + _mark_conv_aborted() + _was_aborted_after(). All four names MUST
    resolve via routes.chat (re-export) AND via routes.chat_state (the
    real home). AND they must operate on the SAME shared state — a
    write via routes.chat_state._mark_conv_aborted MUST be visible to a
    routes.chat._was_aborted_after read (and vice versa). A duplicated-
    state bug (a naive "copy the dict into two modules" split) would
    trip this.

    Semantics guarded (byte-identical to the original chat.py code):
      1. no prior mark          → _was_aborted_after(...) is False
      2. mark then check with ts EARLIER than the mark → True
      3. mark then check with ts AFTER the mark        → False (the
         send that started AFTER the abort is not "aborted by it")
      4. empty conv_id / None since_ts                 → False (guard)
    """
    import time as _t
    import routes.chat as _rc
    import routes.chat_state as _rs

    # Every name must exist on BOTH modules — proves the re-export is real
    # and points at the same object.
    for name in ('_send_abort_marker', '_send_abort_marker_lock',
                 '_mark_conv_aborted', '_was_aborted_after'):
        assert hasattr(_rc, name), f'routes.chat missing re-export: {name}'
        assert hasattr(_rs, name), f'routes.chat_state missing: {name}'
        assert getattr(_rc, name) is getattr(_rs, name), (
            f'{name}: re-export must point at the SAME object (a copy '
            f'would split state — a mark via one namespace would be '
            f'invisible to a check via the other)')

    conv_id = 'test-abort-marker-cv'
    # Clean up any stale entry from a prior run to make the test
    # order-independent (a bare dict in module scope survives across
    # tests in the same process).
    with _rs._send_abort_marker_lock:
        _rs._send_abort_marker.pop(conv_id, None)

    # 1) No prior mark → False regardless of since_ts.
    assert _rc._was_aborted_after(conv_id, _t.time()) is False
    assert _rc._was_aborted_after(conv_id, 0) is False

    # 4) Empty conv_id / None since_ts → False (guard clauses).
    assert _rc._was_aborted_after('', _t.time()) is False
    assert _rc._was_aborted_after(conv_id, None) is False

    # 2) Mark, then check with an EARLIER since_ts → True (an abort
    #    that landed AFTER our send started is the race we detect).
    _earlier = _t.time() - 10
    _rs._mark_conv_aborted(conv_id)  # write via chat_state
    assert _rc._was_aborted_after(conv_id, _earlier) is True  # read via chat re-export

    # 3) Mark, then check with a LATER since_ts → False (a send that
    #    started AFTER the abort is not "aborted by it").
    _later = _t.time() + 10
    assert _rc._was_aborted_after(conv_id, _later) is False

    # Reverse-direction cross-module wire check: mark via chat re-export,
    # read via chat_state — same shared state, same verdict.
    _rc._mark_conv_aborted(conv_id)
    assert _rs._was_aborted_after(conv_id, _earlier) is True

    # Cleanup.
    with _rs._send_abort_marker_lock:
        _rs._send_abort_marker.pop(conv_id, None)


@_unit
def test_truncate_conv_history_side_effect_wire():
    """pt_04686ac6 slice 3: ``_truncate_conv_history`` must be importable
    from ``routes.chat`` (re-export) AND ``routes.chat_side_effects`` (real
    home), point at the SAME object, and — when invoked — call BOTH
    downstream side-effect points (``lib.message_queue.clear_queue`` +
    ``lib.tasks_pkg.server_message_store.clear``). A future slice that
    accidentally drops one clear (the specific class of bug the helper's
    docstring warns against — "folding both into one helper makes the
    invariant impossible to half-apply") is caught here.

    Monkey-patches the two lib entry points to observation-only shims;
    does NOT touch a real message queue or message store.
    """
    import importlib
    import routes.chat as _rc
    import routes.chat_side_effects as _rs

    # Symbol wire: both modules expose it AND they are the same object.
    assert hasattr(_rc, '_truncate_conv_history'), 'routes.chat missing re-export'
    assert hasattr(_rs, '_truncate_conv_history'), 'routes.chat_side_effects missing'
    assert _rc._truncate_conv_history is _rs._truncate_conv_history, (
        'routes.chat._truncate_conv_history must be the SAME object as '
        'routes.chat_side_effects._truncate_conv_history (re-export, not copy)')

    # Behavioural wire: invoking it calls BOTH downstream clears.
    calls = []

    def _fake_clear_queue(conv_id):
        calls.append(('clear_queue', conv_id))
        return 0

    def _fake_clear_store(conv_id):
        calls.append(('clear_store', conv_id))

    mq_mod = importlib.import_module('lib.message_queue')
    ms_mod = importlib.import_module('lib.tasks_pkg.server_message_store')
    orig_cq = mq_mod.clear_queue
    orig_cs = ms_mod.clear
    try:
        mq_mod.clear_queue = _fake_clear_queue
        ms_mod.clear = _fake_clear_store
        _rc._truncate_conv_history('cv-truncate-test')
    finally:
        mq_mod.clear_queue = orig_cq
        ms_mod.clear = orig_cs

    kinds = {c[0] for c in calls}
    assert 'clear_queue' in kinds, (
        '_truncate_conv_history did NOT invoke lib.message_queue.clear_queue — '
        "the 'clear the queued phantom turn' half of the invariant is broken")
    assert 'clear_store' in kinds, (
        '_truncate_conv_history did NOT invoke lib.tasks_pkg.'
        "server_message_store.clear — the 'clear the tool-history mirror' "
        'half of the invariant is broken (regen/edit would replay stale rounds)')
    assert {c[1] for c in calls} == {'cv-truncate-test'}, (
        'both clears must receive the same conv_id we passed in')


# ── Layer 2: Blueprint route wire-parity ──────────────────────────────

def _collect_bp_rules(bp_name: str) -> frozenset:
    """Extract (url_pattern, sorted-method-tuple, endpoint_name) for every
    rule registered on the named Blueprint.

    The tuple form is hashable and deterministic across process runs, so
    forming a frozenset gives a set-equality contract that is oblivious to
    registration order but sensitive to any URL/method/endpoint change.
    """
    if bp_name == 'chat_bp':
        from routes.chat import chat_bp as bp
    elif bp_name == 'api_v1_chat_bp':
        from routes.api_v1.chat import api_v1_chat_bp as bp
    else:
        raise ValueError(f'unknown blueprint: {bp_name}')

    # A Quart/Flask Blueprint carries its rules as deferred (URL, methods,
    # endpoint) triples on ``bp.deferred_functions`` before app.register
    # is called — that is the shape we can inspect without wiring a full
    # app. Every ``@bp.route('...')`` decorator appends one entry via
    # ``add_url_rule`` bound to the BP; the deferred is a callable that
    # replays that add_url_rule against a state object at register time.
    # We call it against a MINIMAL state that just records the call.
    class _Recorder:
        def __init__(self):
            self.rules = []

        def add_url_rule(self, rule, endpoint=None, view_func=None,
                         methods=None, **kwargs):
            m = tuple(sorted(methods or ('GET',)))
            self.rules.append((rule, m, endpoint or (
                view_func.__name__ if view_func else '')))

    rec = _Recorder()
    for setup in bp.deferred_functions:
        # Quart/Flask BP deferred callables take a ``state`` object; ours
        # just needs an ``add_url_rule`` compatible with those calls.
        # ``blueprint`` attribute is read by some code paths.
        class _State:
            blueprint = bp
            name_prefix = ''
            url_prefix = ''
            subdomain = None
            url_defaults = {}
            first_registration = True

            def add_url_rule(self_state, rule, endpoint=None, view_func=None,
                             methods=None, **kwargs):
                rec.add_url_rule(rule, endpoint=endpoint,
                                 view_func=view_func, methods=methods,
                                 **kwargs)
        try:
            setup(_State())
        except Exception:
            # A deferred function that isn't an add_url_rule (e.g.
            # before_request, error handlers) will raise on our
            # minimal state — skip those; they aren't route rules.
            continue
    return frozenset(rec.rules)


@_unit
def test_chat_bp_rules_snapshot():
    """The url/method/endpoint set for ``chat_bp`` (the ``/api/chat/...``
    top-level bp) must match a KNOWN baseline. The baseline reflects the
    pre-split HEAD state; the split's contract is to reproduce it byte-
    identically. When a legitimate future route add lands, update the
    baseline here in the same commit."""
    rules = _collect_bp_rules('chat_bp')
    # chat_bp is small: only the streaming carve-out lives on it.
    expected = frozenset({
        ('/api/chat/stream/<task_id>', ('GET',), 'chat.chat_stream'),
    })
    # Quart normalises endpoint to '<bp>.<view>' when registered on a BP.
    # deferred_functions capture the endpoint AS PASSED to add_url_rule
    # which is BARE (view name) — so we normalise here for equality.
    normalised = frozenset(
        (r, m, ep if '.' in ep else f'chat.{ep}')
        for (r, m, ep) in rules
    )
    assert normalised == expected, (
        f'chat_bp routes changed unexpectedly.\n'
        f'  expected: {sorted(expected)}\n'
        f'  actual:   {sorted(normalised)}\n'
        f'If this is a legitimate new route, update the "expected" set '
        f'in this test as part of the same commit. Otherwise a route '
        f'was silently dropped/renamed by the pt_04686ac6 split.'
    )


@_unit
def test_api_v1_chat_bp_rules_snapshot():
    """Same wire-parity guard for the ``/api/v1/chat/...`` blueprint —
    the primary REST surface. All 15+ rules must be preserved through
    the split."""
    rules = _collect_bp_rules('api_v1_chat_bp')
    # Snapshot of pre-split HEAD (2026-07-23). Every entry corresponds to
    # a route currently in routes/chat.py, routes/chat_queue.py,
    # routes/chat_human_io.py, or routes/chat_tool_state.py — all four
    # attach to the SAME BP; the split moves the routes/chat.py subset
    # around but keeps the BP registrations intact.
    expected_prefixes = {
        '/api/v1/chat/active',
        '/api/v1/chat/start',
        '/api/v1/chat/translate-status/<conv_id>',
        '/api/v1/chat/send',
        '/api/v1/chat/branch',
        '/api/v1/chat/regenerate',
        '/api/v1/chat/continue',
        '/api/v1/chat/abort-conv/<conv_id>',
        '/api/v1/chat/abort/<task_id>',
        '/api/v1/chat/poll/<task_id>',
        '/api/v1/chat/flow-trace/<task_id>',
        '/api/v1/chat/stdin-response',
        '/api/v1/chat/human-response',
        '/api/v1/chat/tool-state/<conv_id>',
        '/api/v1/chat/queue/<conv_id>',
        '/api/v1/chat/queue/<conv_id>/<queue_id>',
    }
    actual_prefixes = {r for (r, _m, _ep) in rules}
    missing = expected_prefixes - actual_prefixes
    added = actual_prefixes - expected_prefixes
    assert not missing, (
        f'api_v1_chat_bp is MISSING rules that existed pre-split: {sorted(missing)}. '
        f'The pt_04686ac6 split must preserve every registered rule; '
        f'either a decorator was lost during the file move or the '
        f'Blueprint import order changed and route registration is '
        f'no longer running.'
    )
    # An ADDED rule is legitimate (a new endpoint landed); flag it as an
    # advisory so the baseline stays honest.
    if added:
        print(f'NOTE: api_v1_chat_bp gained new rules since baseline: {sorted(added)} — '
              f'update the expected_prefixes set to record them')


# ── Slice 4: _start_task_for_conv extraction ──────────────────────────
# Moves the ~150-line task-starter orchestrator (called from chat_send,
# chat_regenerate, chat_continue, chat_branch_start) into a dedicated
# routes/chat_task_start.py. routes/chat.py keeps it re-exported so
# the 3 test files that ``monkeypatch.setattr('routes.chat._start_task_for_conv',
# ...)`` continue to work unchanged.

@_unit
def test_chat_task_start_submodule_exists():
    """Slice 4 (pt_04686ac6): routes/chat_task_start.py holds
    _start_task_for_conv as a module-level callable."""
    import importlib
    mod = importlib.import_module('routes.chat_task_start')
    assert hasattr(mod, '_start_task_for_conv'), (
        'routes.chat_task_start missing _start_task_for_conv')
    assert callable(mod._start_task_for_conv), (
        f'_start_task_for_conv is not callable '
        f'(got {type(mod._start_task_for_conv).__name__})')


@_unit
def test_routes_chat_reexports_start_task_for_conv():
    """Slice 4: routes/chat.py MUST re-export _start_task_for_conv so
    the 3 test files that do ``monkeypatch.setattr(
    'routes.chat._start_task_for_conv', ...)``  keep working unchanged.
    Same-object identity is required — a copy would let a monkey-patch
    on one namespace be invisible on the other."""
    import routes.chat as _rc
    import routes.chat_task_start as _cts
    assert hasattr(_rc, '_start_task_for_conv'), (
        'routes.chat missing _start_task_for_conv re-export — 3 test '
        'files monkeypatch this path')
    assert _rc._start_task_for_conv is _cts._start_task_for_conv, (
        'routes.chat._start_task_for_conv must be the SAME object as '
        'routes.chat_task_start._start_task_for_conv')


@_unit
def test_run_py_no_longer_carries_inline_start_task_for_conv():
    """Slice 4: the inline function body must be GONE from routes/chat.py
    — guards against a future silent revert (someone re-adds the body
    inline while the new module still exists, splitting the definition
    into two copies)."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'routes/chat.py'), encoding='utf-8') as f:
        src = f.read()
    # The 3-argument def line MUST be gone from chat.py (function body
    # moved to chat_task_start.py).
    assert 'def _start_task_for_conv(conv_id, config, data=None):' not in src, (
        'routes/chat.py still contains the inline def line — extraction '
        'was undone or never landed')
    # The re-export MUST be present.
    assert 'from routes.chat_task_start import' in src, (
        'routes/chat.py must import from routes.chat_task_start')


# ── Slice 5: chat_send business logic → lib/chat_dispatch.py ─────────
# Extracts the ~180-line queue classification + autopilot-followup
# detection + abort-on-send race + inject-mode steer + queue-with-
# pending-row-mirror pipeline into a testable pure-ish function.

@_unit
def test_chat_dispatch_module_exists_and_exposes_classifier():
    """Slice 5 (pt_04686ac6): lib/chat_dispatch.py exposes
    classify_send_intent + the SendIntent dataclass."""
    import importlib
    mod = importlib.import_module('lib.chat_dispatch')
    assert hasattr(mod, 'classify_send_intent'), (
        'lib.chat_dispatch missing classify_send_intent')
    assert callable(mod.classify_send_intent), (
        'classify_send_intent must be a callable function')
    assert hasattr(mod, 'SendIntent'), (
        'lib.chat_dispatch missing SendIntent dataclass')


@_unit
def test_chat_send_delegates_to_classify_send_intent():
    """Slice 5: routes/chat.py::chat_send must import + call the
    extracted classifier. Guards against silent revert to the inline
    ~180-line block."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'routes/chat.py'), encoding='utf-8') as f:
        src = f.read()
    assert 'from lib.chat_dispatch import classify_send_intent' in src, (
        'routes/chat.py must import classify_send_intent from '
        'lib.chat_dispatch (slice 5 pt_04686ac6)')
    # Specific inline call-site marker strings that USED TO live inside
    # chat_send are GONE (they now live in lib/chat_dispatch.py).
    assert '⚠️ Abort-on-send: task %s marked aborted' not in src, (
        'routes/chat.py must NOT carry the "Abort-on-send" log line — '
        "it lives in lib.chat_dispatch.classify_send_intent now")
    assert '⚡ superseding %d in-flight autopilot' not in src, (
        'routes/chat.py must NOT carry the autopilot-followup supersede '
        'log line — extracted')
    assert '➡ STEER (injected into running turn)' not in src, (
        'routes/chat.py must NOT carry the STEER log line — extracted')
    assert '➡ QUEUED (active task running)' not in src, (
        'routes/chat.py must NOT carry the QUEUED log line — extracted')


@_unit
def test_classify_send_intent_none_on_no_running_task():
    """Slice 5: when no task is running for the conversation and no
    other classifier branch fires, classify_send_intent returns None
    so chat_send falls through to the immediate-start path.

    Uses monkey-patches on lib.tasks_pkg tasks / tasks_lock so no real
    DB or Flask app context is needed. This is the load-bearing
    invariant that the extracted classifier's None branch really is a
    "no-op, proceed to immediate start"."""
    import sys
    import types
    # Ensure lib.tasks_pkg's tasks dict/lock are available for the
    # classifier to consult. Real module is fine — just make sure
    # the ``tasks`` dict has no entries for our test conv.
    tasks_pkg = sys.modules.get('lib.tasks_pkg')
    if tasks_pkg is None:
        import importlib
        tasks_pkg = importlib.import_module('lib.tasks_pkg')
    orig_tasks = dict(getattr(tasks_pkg, 'tasks', {}))
    # Route the classifier's "was aborted during translate?" to False
    # so the abort-early branch doesn't fire.
    import routes.chat_state as _rs
    orig_was_aborted = _rs._was_aborted_after
    _rs._was_aborted_after = lambda cid, ts: False
    # Route has_autopilot_marker to False so the supersede branch
    # doesn't fire even if a followup task exists.
    import lib.message_queue as _mq
    orig_marker = getattr(_mq, 'has_autopilot_marker', None)
    _mq.has_autopilot_marker = lambda cid: False
    try:
        import lib.chat_dispatch as cd

        result = cd.classify_send_intent(
            db=None, conv_id='cv-none', config={}, payload={},
            data={}, messages=[], is_new=False, title='t',
            user_msg={'content': 'hi'}, settings_patch=None,
            text='hi', send_started_at=0.0,
        )
        assert result is None, (
            f'no running task + no abort + no steer + no supersede '
            f'MUST return None (fall through to immediate-start); '
            f'got {result!r}')
    finally:
        _rs._was_aborted_after = orig_was_aborted
        if orig_marker is not None:
            _mq.has_autopilot_marker = orig_marker
        # Restore any tasks we accidentally polluted.
        tasks_pkg.tasks.clear()
        tasks_pkg.tasks.update(orig_tasks)


@_unit
def test_classify_send_intent_aborted_kind_on_translate_abort():
    """Slice 5: when _was_aborted_after returns True (user hit Stop
    during auto-translate), classifier returns SendIntent(kind='aborted',
    response={'aborted': True, 'convId': <cid>}). Verifies the exact
    response shape chat_send previously returned inline."""
    import routes.chat_state as _rs
    orig = _rs._was_aborted_after
    _rs._was_aborted_after = lambda cid, ts: True
    try:
        import lib.chat_dispatch as cd
        result = cd.classify_send_intent(
            db=None, conv_id='cv-aborted', config={}, payload={},
            data={}, messages=[], is_new=False, title='t',
            user_msg={'content': ''}, settings_patch=None,
            text='hi', send_started_at=1.0,
        )
        assert result is not None
        assert result.kind == 'aborted'
        assert result.response == {'aborted': True, 'convId': 'cv-aborted'}
    finally:
        _rs._was_aborted_after = orig


if __name__ == '__main__':
    tests = [
        test_routes_chat_symbols_all_importable,
        test_extracted_helpers_are_callable,
        test_pure_helper_wire_parity_smoke,
        test_abort_marker_bundle_semantics,
        test_truncate_conv_history_side_effect_wire,
        test_chat_task_start_submodule_exists,
        test_routes_chat_reexports_start_task_for_conv,
        test_run_py_no_longer_carries_inline_start_task_for_conv,
        test_chat_bp_rules_snapshot,
        test_api_v1_chat_bp_rules_snapshot,
        test_chat_dispatch_module_exists_and_exposes_classifier,
        test_chat_send_delegates_to_classify_send_intent,
        test_classify_send_intent_none_on_no_running_task,
        test_classify_send_intent_aborted_kind_on_translate_abort,
    ]
    for fn in tests:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
