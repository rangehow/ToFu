#!/usr/bin/env python3
"""tests/test_frontend_identity_gate_parity.py — the four client-side
multi-user gates must normalize identity IDENTICALLY.

WHY THIS EXISTS (a latent trap, armed by a sibling's work)
----------------------------------------------------------
Four client gates decide whether an inbound ``notify`` frame belongs to this
tab, by comparing ``window._currentUserId`` against ``frame.userId``:

  * ``core/conv_state_reducer.js``  — ``_frameIsOurs`` (SSOT busy channel)
  * ``core/cross_tab_sync.js``      — ``_onConvNotifyPush``
  * ``core/cross_tab_sync.js``      — ``_onFoldersChangedPush``
  * ``conv_sync_push.js``           — ``_onConvSyncPush``

Until now NO JavaScript ever wrote ``window._currentUserId``, so all four were
structurally INERT (``myUser === null`` → accept every frame). Board epic
pt_679d064f68ac4dd6 (owner-chosen wire: a boot ``GET /api/v1/users/me``,
implemented by conv mryjczi2) sets that variable for the first time — which
ARMS all four gates simultaneously.

THE BUG THAT WOULD HAVE BEEN ARMED
----------------------------------
Three of the four compared with a raw strict ``!==``. The server resolves the
frame's ``userId`` through ``routes/common.py::_request_user_id()`` /
``lib/tasks_pkg/manager/_registry.py::task_user_id()``, both of which
int-coerce a numeric id::

    return int(uid) if str(uid).isdigit() else uid

So a tenant whose id is ``'7'`` produces ``frame.userId = 7`` (int), while a
client reading it from a JSON response body holds ``'7'`` (str). ``7 !== '7'``
→ the tab **silently drops its own frames**: cross-device conversation sync,
folder sync and history-rewrite all go dead, with no error anywhere.

``conv_state_reducer.js::_frameIsOurs`` already String()-normalized both sides
and documents exactly this hazard (its pt_ab42421158214591 comment); the other
three never got the same treatment. This suite pins all four in agreement so
the normalization cannot drift back — the failure mode is invisible at
runtime, so a static + behavioural guard is the only thing that catches it.

Scope note: this file deliberately does NOT test the initializer (who WRITES
``_currentUserId``). That is conv mryjczi2's commit. The normalization is
orthogonal — it is required under any wire that sets the variable.

Run isolated (project convention): PYTEST_DISABLE_PLUGIN_AUTOLOAD=1.
"""

from __future__ import annotations

import os
import re

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_GATE_FILES = (
    os.path.join(JS_DIR, 'core', 'conv_state_reducer.js'),
    os.path.join(JS_DIR, 'core', 'cross_tab_sync.js'),
    os.path.join(JS_DIR, 'conv_sync_push.js'),
)


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


# ══════════════════════════════════════════════════════════════════════
#  Static guard — no gate may compare identities without normalizing
# ══════════════════════════════════════════════════════════════════════

def test_no_gate_uses_a_raw_strict_identity_compare():
    """``frame.userId !== myUser`` is a type trap once ``_currentUserId`` is
    actually set: int-vs-str skew makes a tab reject its OWN frames. Every
    identity compare must go through ``String()`` on both sides.

    Presence guards (``!== null`` / ``!== undefined`` / ``!== ""``) are not
    identity compares and are excluded — only a compare that puts the two
    identities against each other can suffer the skew.
    """
    offenders = []
    for path in _GATE_FILES:
        src = _read(path)
        for m in re.finditer(r'^.*(?:userId|_currentUserId).*$', src, re.M):
            line = m.group(0)
            if 'myUser' not in line and '_currentUserId' not in line:
                continue
            if 'String(' in line or 'typeof' in line:
                continue
            stripped = re.sub(r'!==\s*(?:null|undefined|"")', '', line)
            stripped = re.sub(r'===\s*(?:null|undefined|"")', '', stripped)
            if '!==' not in stripped and '===' not in stripped:
                continue
            offenders.append(
                f'{os.path.relpath(path, PROJECT_ROOT)}: {line.strip()}')
    assert not offenders, (
        "raw strict identity compare(s) remain — int/str skew will silently "
        "drop a tab's own frames:\n" + '\n'.join(offenders))


def test_every_gate_normalizes_both_sides():
    """Each of the four gates must String()-normalize BOTH its own identity
    and the frame's — normalizing only one side still skews."""
    # (file, anchor that identifies the gate's normalization block)
    expectations = [
        (os.path.join(JS_DIR, 'core', 'conv_state_reducer.js'),
         'const my = String(myRaw);'),
        (os.path.join(JS_DIR, 'core', 'cross_tab_sync.js'),
         'String(window._currentUserId)'),
        (os.path.join(JS_DIR, 'conv_sync_push.js'),
         'String(window._currentUserId)'),
    ]
    for path, anchor in expectations:
        src = _read(path)
        rel = os.path.relpath(path, PROJECT_ROOT)
        assert anchor in src, f'{rel}: missing own-side normalization ({anchor})'
        assert 'String(frame.userId)' in src or 'String(userId)' in src, (
            f'{rel}: frame-side identity is not String()-normalized')
    # cross_tab_sync carries TWO gates; both must be normalized.
    xts = _read(os.path.join(JS_DIR, 'core', 'cross_tab_sync.js'))
    assert xts.count('String(window._currentUserId)') >= 2, (
        'cross_tab_sync.js has two gates (_onConvNotifyPush + '
        '_onFoldersChangedPush); both must normalize')


# ══════════════════════════════════════════════════════════════════════
#  Behavioural — drive the REAL shipped predicate under jsdom
# ══════════════════════════════════════════════════════════════════════

_GATE_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[2]],
  globals: { activeStreams: new Map(), conversations: [], debugLog: () => {} },
});

// The reducer's gate is the reference implementation the other three mirror.
const isOurs = window._frameIsOurs || _frameIsOurs;
check('gate_exported', typeof isOurs === 'function');

// ── Unscoped default (no identity established) accepts everything. This is
//    the pre-auth / personal-install path and MUST stay byte-identical. ──
window._currentUserId = null;
check('unset_accepts_int',    isOurs(1) === true);
check('unset_accepts_string', isOurs('alice') === true);
check('unset_accepts_absent', isOurs(undefined) === true);

// ── int/str skew MUST NOT drop our own frame (the whole point) ──
window._currentUserId = 1;
check('int_id_accepts_str_frame', isOurs('1') === true);
window._currentUserId = '1';
check('str_id_accepts_int_frame', isOurs(1) === true);

// ── A genuinely foreign tenant IS still dropped (the gate must not become
//    a no-op in the name of leniency) ──
window._currentUserId = 'alice';
check('foreign_tenant_dropped', isOurs('bob') === false);
check('own_tenant_accepted',    isOurs('alice') === true);

// ── An unscoped frame (server had no tenant) still passes a scoped tab —
//    covers the 5 background write sites deliberately left unmigrated by
//    pt_abae3a85a92440fd, which emit frames with the default identity. ──
check('empty_frame_accepted', isOurs('') === true);

report();
"""


def test_reference_gate_behaviour():
    """Drive the REAL shipped predicate: int/str skew accepted, foreign tenant
    dropped, unscoped-on-either-side accepted."""
    run_harness(
        target_js=os.path.join(JS_DIR, 'core', 'conv_state_reducer.js'),
        body_js=_GATE_BODY,
        min_pass=9,
        label='identity gate',
    )


def test_NEUTER_unnormalized_gate_drops_own_frame():
    """NEUTER: revert the reference gate to the raw strict compare the other
    three sites used. The int/str skew cases must flip RED — proving the
    normalization (not the assertion) is what keeps a tab's frames flowing.
    """
    import subprocess
    import tempfile

    from tests._jsdom import ROOT, node_deps_available

    if not node_deps_available():
        pytest.skip('node + jsdom dev-deps not installed')

    src_path = os.path.join(JS_DIR, 'core', 'conv_state_reducer.js')
    src = _read(src_path)
    normalized = '''  const my = String(myRaw);
  const theirs = String(userId);
  if (my === '' || theirs === '') return true;
  return theirs === my;'''
    assert normalized in src, 'normalization block not found — did the gate change?'
    neutered = src.replace(normalized, '  return userId === myRaw;', 1)
    assert neutered != src

    neutered_path = harness = None
    try:
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(src_path), delete=False,
            encoding='utf-8',
        ) as fh:
            neutered_path = fh.name
            fh.write(neutered)
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(os.path.abspath(__file__)),
            delete=False, encoding='utf-8',
        ) as hf:
            harness = hf.name
            hf.write(_GATE_BODY)
        proc = subprocess.run(
            ['node', harness, neutered_path, ROOT],
            capture_output=True, text=True, timeout=60,
            env={**os.environ,
                 'JSDOM_HARNESS': os.path.join(
                     os.path.dirname(os.path.abspath(__file__)),
                     '_jsdom_harness.js')},
        )
        out = (proc.stdout or '').strip()
        assert ('FAIL int_id_accepts_str_frame' in out
                or 'FAIL str_id_accepts_int_frame' in out), (
            'NEUTER did not bite — the un-normalized gate should drop a '
            'type-skewed own-frame:\n' + out)
    finally:
        for p in (neutered_path, harness):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass
