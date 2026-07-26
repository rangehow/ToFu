#!/usr/bin/env python3
"""pt_conv_state_ssot P6 VERDICT GUARD — the three branches must NOT be deleted.

WHY THIS FILE EXISTS
--------------------
P6 as originally scoped says "sweep 3 redundant branches, each deletion
commit-body naming which new frame replaces its old scenario" (owner hard
constraint #5). Working it produced the opposite finding: **there is no such
frame for any of the three**, so the sweep must not happen. This guard pins
that verdict in executable form, because the epic text still reads like a
deletion task and the next session to pick it up would otherwise re-derive
(or worse, skip) the analysis and delete live safety nets.

THE ARGUMENT, IN ONE LINE
-------------------------
P2 BIFURCATED busy state (owner hard constraint #2):

    conv.activeTaskId                 -> LOCAL OPTIMISTIC   (this tab's send)
    conv._authoritativeActiveTaskIds  -> SERVER AUTHORITATIVE (frames only)

``computeConvBusy`` reads the UNION. The new conv-state channel — notify
frames + connect snapshot — writes and clears ONLY the authoritative half.
Nothing in the new channel can clear a stale LOCAL OPTIMISTIC pin. The three
"redundant" branches are precisely what covers that half plus the offline
case, so deleting them re-opens the symptom the epic was created to fix
("sidebar busy dot outlives the work" / phone shows N generating, PC fewer).

WHY THE task_ids DRIFT EVIDENCE DOES NOT AUTHORIZE DELETION
------------------------------------------------------------
The P5 probe's ``taskIds`` dimension reads ``_authoritativeActiveTaskIds``.
Sustained zero divergence there proves the AUTHORITATIVE half converges — it
says nothing about the optimistic half. Using it as the admission criterion
for deleting an optimistic-half safety net is a category error, and was the
plan until this analysis. Measured: 8 consecutive probe cycles, zero
``kind=task_ids`` divergences, while the optimistic-pin sweep remained
load-bearing.

Run: python3 tests/test_conv_state_p6_verdict.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CROSS_TAB = os.path.join(REPO, 'static', 'js', 'core', 'cross_tab_sync.js')
HEALTH = os.path.join(REPO, 'static', 'js', 'core', 'health_stream_timer.js')
REDUCER = os.path.join(REPO, 'static', 'js', 'core', 'conv_state_reducer.js')

try:
    import pytest
except ImportError:
    pytest = None


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _strip_comments(src):
    """Drop /* */ and // so 'is it CALLED' can't be satisfied by prose."""
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    src = re.sub(r'^\s*//.*$', '', src, flags=re.M)
    return src


# ── Branch 1: the conv-agnostic stale optimistic-pin sweep ───────────────

@_unit
def test_stale_pin_sweep_is_defined_AND_actually_invoked():
    """Both must hold. A definition with no call site is a silent hole.

    (An earlier pass mis-reported this function as uncalled dead code after a
    grep whose parenthesis was swallowed. Asserting BOTH facts here makes that
    mistake impossible to repeat.)
    """
    code = _strip_comments(_read(CROSS_TAB))
    assert 'function _reconcileStuckActiveTaskPins' in code, (
        'the optimistic-pin sweep was DELETED — P6 verdict is that it must '
        'stay; see this file\'s docstring')
    # A call is any occurrence that is not the definition itself.
    calls = [m for m in re.finditer(r'_reconcileStuckActiveTaskPins\s*\(', code)
             if not code[:m.start()].rstrip().endswith('function')]
    assert calls, (
        'the sweep is defined but never invoked — the background orphan-pin '
        'lane is silently unguarded')


@_unit
def test_sweep_clears_the_OPTIMISTIC_field_not_the_authoritative_one():
    """This is the whole reason the branch is not redundant."""
    code = _read(CROSS_TAB)
    start = code.index('function _reconcileStuckActiveTaskPins')
    body = code[start:start + 4000]
    assert 'activeTaskId' in body, (
        'the sweep must operate on the LOCAL OPTIMISTIC pin (conv.activeTaskId)')
    assert '_authoritativeActiveTaskIds' not in body, (
        'if this sweep ever starts writing the AUTHORITATIVE set it becomes a '
        'second writer of server-owned state, violating hard constraint #3')


# ── Branch 2: the shared background reclaim it delegates into ────────────

@_unit
def test_background_orphan_reclaim_still_present():
    code = _read(HEALTH)
    assert 'Background orphan-pin clear' in code, (
        'the background reclaim arm was removed; the sweep delegates into it, '
        'so deleting it guts branch 1 as well')
    assert 'probe.background' in _strip_comments(code), (
        'the background gate is what keeps the FOREGROUND timer path '
        'byte-identical — losing it changes live-stream behaviour')


# ── Branch 3: offline recovery polling ──────────────────────────────────

@_unit
def test_offline_recovery_polling_still_present():
    code = _strip_comments(_read(CROSS_TAB))
    assert 'function _startOfflineRecoveryPolling' in code, (
        'offline recovery polling was deleted — when the server is offline the '
        'new frame channel cannot deliver either, so nothing replaces it')


# ── The load-bearing asymmetry itself ───────────────────────────────────

@_unit
def test_new_channel_clears_only_the_authoritative_half():
    """Pins the fact the whole verdict rests on.

    If a future change makes the snapshot clear ``activeTaskId`` too, THEN the
    branches genuinely become redundant and P6 can be revisited — this test
    failing is the signal to re-open that question, not to delete the guard.
    """
    code = _read(REDUCER)
    start = code.index('function applyConvStateSnapshot')
    body = code[start:start + 4000]
    assert '_authoritativeActiveTaskIds = new Set()' in body, (
        'snapshot clear semantics changed; re-derive the P6 verdict')
    assert not re.search(r'\bconv\.activeTaskId\s*=', body), (
        'the snapshot now writes the OPTIMISTIC pin. That would change the P6 '
        'verdict (branches may become redundant) AND violate hard constraint '
        '#2 (fields never merge). Re-open the analysis deliberately.')


if __name__ == '__main__':
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('ok  ', name)
            except AssertionError as e:
                failures += 1
                print('FAIL', name)
                print('     ', e)
    print('ALL PASSED' if not failures else f'{failures} FAILED')
    sys.exit(1 if failures else 0)
