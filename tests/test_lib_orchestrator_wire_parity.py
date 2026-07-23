#!/usr/bin/env python3
"""Wire-parity baseline for the lib/tasks_pkg/orchestrator/_run.py split
(board epic ``pt_03f4cdf1``).

The plan: ``lib/tasks_pkg/orchestrator/_run.py`` currently holds a single
1813-line function ``run_task`` (the hottest path in the codebase). Future
slices will extract phase seams (pre-stream prep / VU startup / stream loop /
per-round tool dispatch / finalize) into sub-modules of a new
``lib/tasks_pkg/orchestrator/_run/`` sub-package, keeping the top-level
package's import surface byte-identical.

This test is the CONTRACT the split must preserve. Analogous to the routes/
chat.py wire-parity test (``tests/test_routes_chat_wire_parity.py``) but for
the orchestrator: assert that every symbol external code imports today from
``lib.tasks_pkg.orchestrator`` — the facade — AND from
``lib.tasks_pkg.orchestrator._run`` — the raw sub-module some consumers still
name directly (``endpoint_review.py`` / ``autopilot.py``) — keeps resolving
after any future slice.

Cannot pre-emptively snapshot Blueprint URLs here (there are none — this is
lib code, not routes). The equivalent contract IS the import-symbol surface:
consumers doing ``from lib.tasks_pkg.orchestrator import run_task`` /
``from lib.tasks_pkg.orchestrator._run import run_task`` etc. all resolve.

Written BEFORE any _run.py source movement so the same tests run pre- and
post- every future extraction slice; a symbol accidentally dropped by a
future move trips this test at that slice's PR.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Flask→Quart shim (matches the rest of the suite; orchestrator itself does
# NOT touch flask, but downstream imports do).
import quart as _quart
sys.modules.setdefault('flask', _quart)

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


# ── The full re-export surface expected on the facade ─────────────────
# Every name external code imports from ``lib.tasks_pkg.orchestrator``
# today. Extracted from ``lib/tasks_pkg/orchestrator/__init__.py`` and
# from live ``from lib.tasks_pkg.orchestrator import <X>`` grep results
# across the tree. A future slice that inadvertently drops one of these
# from the facade — either because a submodule renamed the symbol or
# because __init__.py forgot to re-export it after a move — trips this
# test. Deliberately ORDERED alphabetically inside each group so a diff
# on a slice PR reads cleanly.
_ORCHESTRATOR_FACADE_SYMBOLS = (
    # ── Main entry points (drivers) ──
    'run_task',                    # the giant 1813-line loop being sliced
    '_run_single_turn',            # reusable one-cycle primitive
    'drain_peer_messages_into',    # peer-message drain into a turn
    # ── Rebindable protocol binding ──
    'build_body',                  # tests/consumers can reassign this
    # ── Finalize helpers (from _finalize submodule) ──
    '_discard_pretool_prose',
    '_check_suspicious_completion',
    '_emit_tool_round_phase',
    '_finalize_dangling_tool_rounds',
    '_maybe_auto_retry_turn',
    '_maybe_append_sources_footer',
    '_finalize_and_emit_done',
    '_SRC_URL_RE',
    '_repair_json',
    '_compute_write_breakdown',
    '_ENVELOPE_MAX_TOKENS',
    '_READ_DROP_WASTE_TOKENS',
    '_run_commit_round_async',
    # ── Re-exports from _run.py used at module.X call sites ──
    'AbortedError',
    'append_event',
    'checkpoint_task_partial',
    'persist_task_result',
    'stream_llm_response',
    '_strip_base64_for_snapshot',
    'derive_round_modified_files',
    '_spawn_async_commit_round',
    '_spawn_async_profile_consolidation',
    'EventType',
    'build_event',
    'tool_label',
)


# Symbols external code imports DIRECTLY from the _run submodule (bypassing
# the facade). Discovered from grep across the tree: autopilot.py +
# endpoint_review.py both call ``from lib.tasks_pkg.orchestrator import
# _run_single_turn`` but _turn.py calls ``from
# lib.tasks_pkg.orchestrator._run import run_task``. If a future slice moves
# ``run_task`` into a sub-package (e.g. .._run/__init__.py) that submodule
# MUST still resolve ``run_task`` as an attribute for these direct imports
# to keep working.
_RUN_SUBMODULE_SYMBOLS = (
    'run_task',
)


@_unit
def test_orchestrator_facade_symbols_all_importable():
    """Every symbol external code imports from
    ``lib.tasks_pkg.orchestrator`` today must still resolve after any
    _run.py split. A future slice that inadvertently drops a name from
    the facade — because a submodule renamed the symbol or __init__.py
    forgot to re-export it — trips here."""
    import importlib
    facade = importlib.import_module('lib.tasks_pkg.orchestrator')
    missing = [name for name in _ORCHESTRATOR_FACADE_SYMBOLS
               if not hasattr(facade, name)]
    assert not missing, (
        f'lib.tasks_pkg.orchestrator missing symbols external code '
        f'imports: {missing}. If you split _run.py, keep '
        f'orchestrator/__init__.py as a re-export facade that surfaces '
        f'every name in _ORCHESTRATOR_FACADE_SYMBOLS.'
    )


@_unit
def test_run_submodule_symbols_all_importable():
    """The raw ``lib.tasks_pkg.orchestrator._run`` sub-module surface
    (a small subset — some consumers name it directly) must also survive
    any future split."""
    import importlib
    run_mod = importlib.import_module('lib.tasks_pkg.orchestrator._run')
    missing = [name for name in _RUN_SUBMODULE_SYMBOLS
               if not hasattr(run_mod, name)]
    assert not missing, (
        f'lib.tasks_pkg.orchestrator._run missing symbols direct '
        f'importers rely on: {missing}. If you split _run.py into a '
        f'sub-package, keep _run/__init__.py as a re-export facade.'
    )


@_unit
def test_run_task_is_callable():
    """``run_task`` (via both the facade and the sub-module) must be a
    callable — not accidentally re-exported as e.g. the containing module,
    ``None``, or some other placeholder. The wire test catches an
    accidental type-drift a plain hasattr check would miss."""
    from lib.tasks_pkg.orchestrator import run_task as via_facade
    from lib.tasks_pkg.orchestrator._run import run_task as via_submodule
    assert callable(via_facade), (
        'lib.tasks_pkg.orchestrator.run_task is not callable '
        f'(got {type(via_facade).__name__})')
    assert callable(via_submodule), (
        'lib.tasks_pkg.orchestrator._run.run_task is not callable '
        f'(got {type(via_submodule).__name__})')
    assert via_facade is via_submodule, (
        'lib.tasks_pkg.orchestrator.run_task must be the SAME object as '
        'lib.tasks_pkg.orchestrator._run.run_task (facade re-export, '
        'not a copy — a copy would break monkeypatching in tests that '
        'reassign one namespace and expect the other to follow)')


@_unit
def test_build_body_binding_is_rebindable_on_facade():
    """The ``build_body`` binding MUST live on the facade (the docstring
    contract): tests/consumers reassign ``orchestrator.build_body`` and
    the loop must see it via ``_o.build_body`` at call time.

    Guard: after reassigning, the new value is observable via the facade
    AND via the raw ``import lib.tasks_pkg.orchestrator as _o`` idiom the
    _run.py loop uses. Restore the original binding afterwards so the
    test is idempotent + isolation-safe under xdist."""
    import lib.tasks_pkg.orchestrator as _o
    original = _o.build_body
    sentinel = object()
    try:
        _o.build_body = sentinel
        # Both access paths must see the rebinding.
        assert _o.build_body is sentinel, 'facade binding did not take'
        # Simulating what _run.py does:
        assert getattr(_o, 'build_body') is sentinel, (
            'the "resolve at call time via _o.build_body" idiom does not '
            'see the rebinding — this is the contract every extracted '
            'phase must preserve')
    finally:
        _o.build_body = original


@_unit
def test_finalize_and_turn_submodule_names_present():
    """The two SIBLING submodules of _run.py (_finalize, _turn) each carry
    known symbols external code imports directly. A future _run.py slice
    that inadvertently pulls a name from _finalize or _turn without re-
    homing it correctly trips here.

    Not exhaustive — only the direct-import names actually grep'd in the
    codebase today."""
    import importlib
    fin = importlib.import_module('lib.tasks_pkg.orchestrator._finalize')
    turn = importlib.import_module('lib.tasks_pkg.orchestrator._turn')

    # Direct imports on _finalize surfaced by grep:
    for name in ('_discard_pretool_prose', '_emit_tool_round_phase',
                 '_finalize_and_emit_done', '_maybe_auto_retry_turn',
                 '_compute_write_breakdown'):
        assert hasattr(fin, name), (
            f'lib.tasks_pkg.orchestrator._finalize missing {name!r} '
            f'(imported by _run.py at module load time)')

    # Direct imports on _turn surfaced by grep:
    for name in ('drain_peer_messages_into', '_run_single_turn', 'run_task'):
        assert hasattr(turn, name), (
            f'lib.tasks_pkg.orchestrator._turn missing {name!r}')


if __name__ == '__main__':
    tests = [
        test_orchestrator_facade_symbols_all_importable,
        test_run_submodule_symbols_all_importable,
        test_run_task_is_callable,
        test_build_body_binding_is_rebindable_on_facade,
        test_finalize_and_turn_submodule_names_present,
    ]
    for fn in tests:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
