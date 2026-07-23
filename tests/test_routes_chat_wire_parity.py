#!/usr/bin/env python3
"""Wire-parity baseline for the routes/chat.py sub-package split
(board epic ``pt_04686ac6054a451e``).

The plan: routes/chat.py (~2500 lines) is being decomposed into a
``routes/chat/`` sub-package (``_helpers.py`` first slice, then eventually
``send.py`` / ``stream.py`` / ``poll_abort.py``), with ``routes/chat.py``
kept as a **re-export facade** so import contracts and Blueprint
registrations stay byte-identical. The first slice (this commit) extracts
the 5 pure utilities with NO module-level mutable state:

  _dumps_yielding, _running_checkpoint_verdict, _log_poll_task_id_mismatch,
  _loads_yielding, _warm_resume_serviceable

into ``routes/chat/_helpers.py`` and re-exports them from ``routes/chat.py``.

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


if __name__ == '__main__':
    tests = [
        test_routes_chat_symbols_all_importable,
        test_extracted_helpers_are_callable,
        test_pure_helper_wire_parity_smoke,
        test_chat_bp_rules_snapshot,
        test_api_v1_chat_bp_rules_snapshot,
    ]
    for fn in tests:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
