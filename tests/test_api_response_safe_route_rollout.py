#!/usr/bin/env python3
"""@safe_route rollout tests (routes/api_v1/optimizer.py, pt_63eb7f02 batch 3).

The mechanical-sweep epic pt_63eb7f02 has TWO halves: (1) migrate ad-hoc
``jsonify({'error': ...})`` to ``api_error(...)`` — batches 1+2 landed via
2ded48cc / 825338e8. (2) Adopt ``@safe_route`` on handlers whose except-block is
a pure ``logger.error(...); return api_internal_error(e, context=<fn>, ...)``
with no side effects — THIS test targets that half.

The three handlers converted this batch — ``list_proposals``, ``get_proposal``,
``run_now`` in ``routes/api_v1/optimizer.py`` — met the strict conversion gate:

  1. except-block was PURE (logger.error + api_internal_error), no
     side effects like ``_storage.update_proposal_status(...)`` or
     ``audit_log(...)`` (those would be silently dropped by a bare
     ``@safe_route``).
  2. context= string was equivalent to what ``safe_route`` builds
     automatically from ``fn.__qualname__`` / ``fn.__module__`` — so
     diagnostic granularity is preserved after conversion.

Deliberately NOT converted this batch: ``approve_proposal`` (its except runs
``_storage.update_proposal_status(reason=...)`` + ``audit_log(...)``) and
``revert_proposal`` (its except uses ``context='revert.apply'`` distinct from
the qualname). Those are shape-sensitive; a partial ``@safe_route`` would drop
their diagnostic side effects. Guarded here so a future well-meaning cleanup
does not silently convert them without noticing.

Two test layers, mirroring the pattern used by
``tests/test_api_response_route_conversions.py``:

  1. WIRE-SAFETY parity — the decorated handlers still return a 500 with the
     ``ok:False`` envelope shape when they raise. Uses the real
     ``lib.api_response.safe_route`` decorator through a fabricated handler
     (the real ones require _storage + auth) so we prove decorator behaviour,
     not the specific handler's plumbing.
  2. SHIPPED-SOURCE regression — the three converted handlers ARE decorated
     with @safe_route and their old ``try / except Exception / api_internal_error``
     hand-roll is GONE. The two intentionally-not-converted handlers still
     carry their audit-log / status-flip side effects.
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Flask→Quart shim (matches the rest of the suite).
import quart as _quart
sys.modules.setdefault('flask', _quart)

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OPTIMIZER_PATH = os.path.join(_ROOT, 'routes', 'api_v1', 'optimizer.py')


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


def _make_app():
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}
    return Quart(__name__)


# ── Layer 1: decorator-level wire-safety ──────────────────────────────

@_unit
def test_safe_route_wraps_sync_handler_500_envelope():
    """@safe_route around a sync handler that raises returns a 500 whose
    body carries ``ok:False`` + an ``error`` envelope (the api_error contract).

    This is the exact contract the converted optimizer.py handlers now rely on
    instead of the hand-rolled try/except. If safe_route ever regressed to
    letting the exception propagate or dropping the ``ok`` key, the migrated
    optimizer handlers would silently break — this test is the tripwire.
    """
    from lib.api_response import safe_route

    @safe_route
    def crashing():
        raise ValueError('boom in list')

    app = _make_app()

    async def _t():
        async with app.test_request_context('/x'):
            resp, status = crashing()
            assert status == 500, f'expected 500, got {status}'
            body = json.loads(await resp.get_data(as_text=True))
            assert body.get('ok') is False, f'missing ok:False in {body!r}'
            assert 'error' in body, f'missing error key in {body!r}'

    import asyncio
    asyncio.run(_t())


@_unit
def test_safe_route_passthroughs_a_successful_response():
    """@safe_route on a handler that does NOT raise passes its return through
    verbatim. Guards against a regression where safe_route accidentally
    reshapes 2xx responses (which would break every converted handler's happy
    path — list_proposals/get_proposal/run_now all return api_ok(...))."""
    from lib.api_response import api_ok, safe_route

    @safe_route
    def happy():
        return api_ok({'value': 42})

    app = _make_app()

    async def _t():
        async with app.test_request_context('/x'):
            resp, status = happy()
            assert status == 200
            body = json.loads(await resp.get_data(as_text=True))
            assert body.get('ok') is True
            assert body.get('value') == 42

    import asyncio
    asyncio.run(_t())


# ── Layer 2: shipped-source regression ────────────────────────────────

# The trio being converted this batch. Each entry: (fn_name, expect_decorated,
# expect_no_ad_hoc_try). "Ad-hoc try" here = the specific "try _storage.X /
# except Exception / return api_internal_error(...)" hand-roll — a
# per-handler regex, not a blanket ban on all try/except (the file legitimately
# still has JSON-decode try/except).
_CONVERTED_HANDLERS = ('list_proposals', 'get_proposal', 'run_now')

# Handlers explicitly NOT converted this batch — their except blocks do
# side-effectful work (audit_log / status-flip) or use a distinct context=
# string. Guarded so a future well-meaning cleanup that "just adds
# @safe_route to the remaining ones" trips this test.
_UNCONVERTED_HANDLERS = ('approve_proposal', 'revert_proposal')


def _optimizer_src() -> str:
    with open(_OPTIMIZER_PATH, encoding='utf-8') as f:
        return f.read()


@_unit
def test_converted_handlers_are_decorated_with_safe_route():
    """Each of list_proposals / get_proposal / run_now MUST carry
    @safe_route immediately above its ``def`` line — the source-level
    regression tripwire."""
    src = _optimizer_src()
    for name in _CONVERTED_HANDLERS:
        # Match: @safe_route on its own line, followed by (optional
        # blank / comment lines), then `def <name>(`. The decorator MUST
        # be the innermost one relative to the function so route-registration
        # decorators still fire first — but pattern-wise it can appear
        # anywhere in the decorator stack.
        pattern = re.compile(
            r'@safe_route\b[^\n]*\ndef\s+' + re.escape(name) + r'\s*\(',
            re.MULTILINE)
        assert pattern.search(src), (
            f'{name}: expected @safe_route immediately before its def line '
            f'in routes/api_v1/optimizer.py — the pt_63eb7f02 batch-3 '
            f'rollout hasn\'t landed or has been undone')


@_unit
def test_converted_handlers_no_longer_hand_roll_the_500_try_except():
    """The ad-hoc ``try: <storage-call> ... except Exception ... return
    api_internal_error(...)`` block is GONE from each converted handler.

    Method: bracket each handler's source between its ``def`` and either
    the next top-level ``def``/``@api_v1_optimizer_bp.route`` or EOF,
    then assert that no ``except Exception`` with an ``api_internal_error``
    call appears inside that slice.
    """
    src = _optimizer_src()

    # Locate handler boundaries: sorted def offsets → each handler runs
    # from its def line to just before the next @route decorator (or EOF).
    def_offsets = {}
    for m in re.finditer(r'^def\s+(\w+)\s*\(', src, re.MULTILINE):
        def_offsets[m.group(1)] = m.start()

    # Sort by offset to derive slices.
    sorted_defs = sorted(def_offsets.items(), key=lambda kv: kv[1])
    slices = {}
    for i, (name, off) in enumerate(sorted_defs):
        end = sorted_defs[i + 1][1] if i + 1 < len(sorted_defs) else len(src)
        slices[name] = src[off:end]

    for name in _CONVERTED_HANDLERS:
        slc = slices.get(name, '')
        assert slc, f'could not locate {name} slice — parser drift?'
        # The specific ad-hoc pattern being removed:
        # `except Exception as e: ... return api_internal_error(`
        # We look for those two tokens in the same slice; a legitimate
        # non-500 try/except (e.g. JSON decode) will not carry an
        # api_internal_error return.
        has_except_exception = re.search(
            r'except\s+Exception\s+as\s+\w+\s*:', slc) is not None
        has_internal_error_return = 'api_internal_error(' in slc
        assert not (has_except_exception and has_internal_error_return), (
            f'{name}: still contains the ad-hoc "except Exception → '
            f'api_internal_error(" pattern that @safe_route was meant '
            f'to replace. The decorator adds this behaviour automatically; '
            f'the manual block is redundant and diverges from the '
            f'pt_63eb7f02 batch-3 goal.\n'
            f'Slice excerpt:\n{slc[:600]}')


@_unit
def test_unconverted_handlers_keep_their_side_effect_except_blocks():
    """approve_proposal / revert_proposal were deliberately NOT converted:
    their except blocks do meaningful work that a plain @safe_route would
    silently drop. Guard so a future overzealous cleanup does not remove
    them without noticing.

    approve_proposal: except → _storage.update_proposal_status(reason=...) +
                      audit_log('optimizer_action_failed', ...)
    revert_proposal:  except → api_internal_error(e, context='revert.apply',
                      source='api_v1.optimizer.revert') — the context= string
                      distinguishes revert.apply from other revert paths and
                      would collapse to fn.__qualname__ under @safe_route.
    """
    src = _optimizer_src()

    # approve_proposal: must still call update_proposal_status with a
    # reason string that mentions "manual approve failed" (the failure
    # side effect that @safe_route CANNOT reproduce).
    approve_slice = _slice_of(src, 'approve_proposal')
    assert "manual approve failed" in approve_slice, (
        'approve_proposal: expected the "manual approve failed" audit '
        'reason to survive — this side effect is the reason it was not '
        'converted to @safe_route in batch 3')
    assert 'optimizer_action_failed' in approve_slice, (
        'approve_proposal: expected the audit_log("optimizer_action_failed") '
        'call to survive — same reason')

    # revert_proposal: must still carry the distinct context='revert.apply'
    # string; @safe_route would collapse this to the qualname
    # 'revert_proposal', losing the "which step failed" resolution.
    revert_slice = _slice_of(src, 'revert_proposal')
    assert "context='revert.apply'" in revert_slice, (
        "revert_proposal: expected context='revert.apply' string to "
        'survive — it distinguishes the revert-step failure from '
        'other revert-path errors and would collapse under @safe_route')


def _slice_of(src: str, name: str) -> str:
    """Extract a function-body slice from a top-level def to the next
    def / route decorator / EOF. Small helper used above."""
    defs = [(m.group(1), m.start())
            for m in re.finditer(r'^def\s+(\w+)\s*\(', src, re.MULTILINE)]
    defs.sort(key=lambda kv: kv[1])
    for i, (n, off) in enumerate(defs):
        if n == name:
            end = defs[i + 1][1] if i + 1 < len(defs) else len(src)
            return src[off:end]
    return ''


@_unit
def test_metadata_lists_stay_in_sync():
    """Meta-test: both _CONVERTED_HANDLERS and _UNCONVERTED_HANDLERS are
    non-empty and disjoint. Guards against an accidental copy-paste
    (same name in both lists → the assertions cancel out silently)."""
    assert _CONVERTED_HANDLERS, 'converted list must not be empty'
    assert _UNCONVERTED_HANDLERS, 'unconverted list must not be empty'
    overlap = set(_CONVERTED_HANDLERS) & set(_UNCONVERTED_HANDLERS)
    assert not overlap, f'a handler cannot be both converted and not: {overlap}'


if __name__ == '__main__':
    tests = [
        test_safe_route_wraps_sync_handler_500_envelope,
        test_safe_route_passthroughs_a_successful_response,
        test_converted_handlers_are_decorated_with_safe_route,
        test_converted_handlers_no_longer_hand_roll_the_500_try_except,
        test_unconverted_handlers_keep_their_side_effect_except_blocks,
        test_metadata_lists_stay_in_sync,
    ]
    for fn in tests:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
