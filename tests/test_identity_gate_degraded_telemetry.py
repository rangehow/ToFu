#!/usr/bin/env python3
"""tests/test_identity_gate_degraded_telemetry.py — the fail-open degrade must
reach the SERVER, not just a browser console.

WHY
---
``conv_state_reducer::_frameIsOurs`` is the ONE multi-user identity predicate;
three consumer gates delegate to it and fail OPEN when it is missing (a
build-order regression). 25dd7b19 made that degrade visible via a one-shot
``console.warn`` — but console output is BROWSER-LOCAL. Nobody operating the
server ever learns that a page is accepting every notify frame unscoped.

Every other invariant in this subsystem already reports home: the P5 sync-drift
probe POSTs a digest every 60s and the server WARN-logs divergences. The one
security-relevant degrade was the only signal that did not use that path.

So the flag rides the EXISTING probe — no new endpoint, no new cadence, no new
failure mode, and the degrade lands in ``logs/app.log`` beside every other
drift signal.

THE SUBTLE PART (and the reason this file exists)
-------------------------------------------------
``reportSyncDigest`` used to bail on ``if (!digests.length) return null``. A
page whose bundle order broke can easily have ZERO authoritative markers —
arguably the LIKELIEST shape of the bug, since a missing reducer means nothing
ever wrote ``_authoritativeActiveTaskIds``. Gating the POST on a non-empty
digest list would therefore have suppressed the signal on exactly the page it
exists to catch. The send condition is now ``digests.length || degraded``, and
the server reads the flag BEFORE validating ``digests`` for the same reason.

Telemetry only: the flag never influences an accept/reject decision — the
fail-open is already decided by the time it is set.

Run isolated (project convention): PYTEST_DISABLE_PLUGIN_AUTOLOAD=1.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

_REDUCER = os.path.join(JS_DIR, 'core', 'conv_state_reducer.js')


# ══════════════════════════════════════════════════════════════════════
#  Client — the flag rides the existing digest POST
# ══════════════════════════════════════════════════════════════════════

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[2]],
  globals: { activeStreams: new Map(), conversations: [], debugLog: () => {} },
});

let posted = [];
window.Api = global.Api = {
  conversations: {
    reportSyncDigest: (digests, extra) => {
      posted.push({ digests: digests, extra: extra });
      return Promise.resolve({ ok: true, checked: digests.length, divergences: [] });
    },
  },
};

const settle = () => new Promise((r) => setImmediate(r));

(async () => {
  /* ── 1. Healthy gate + real conv state → digest sent, NO flag ── */
  window.resetIdentityGateWarnedForTests();
  posted = [];
  const convs = [{ id: 'c1', _serverRev: 4,
                   _authoritativeActiveTaskIds: new Set(['t1']),
                   _authoritativeActiveTaskIdsRev: [10, 'r'] }];
  await window.reportSyncDigest(convs);
  await settle();
  check('healthy_posted', posted.length === 1);
  check('healthy_has_digest', posted[0] && posted[0].digests.length === 1);
  check('healthy_no_flag',
        !posted[0].extra || posted[0].extra.identityGateDegraded !== true);

  /* ── 2. Healthy gate + NO conv state → nothing sent (unchanged: the
         probe must stay silent on an idle page) ── */
  window.resetIdentityGateWarnedForTests();
  posted = [];
  await window.reportSyncDigest([]);
  await settle();
  check('healthy_empty_silent', posted.length === 0);

  /* ── 3. DEGRADED + real conv state → flag rides the digest ── */
  window.resetIdentityGateWarnedForTests();
  posted = [];
  window.reportIdentityGateUnavailable('_onConvNotifyPush');
  check('tripwire_latched', window.identityGateDegraded() === true);
  await window.reportSyncDigest(convs);
  await settle();
  check('degraded_posted', posted.length === 1);
  check('degraded_flag_set',
        posted[0].extra && posted[0].extra.identityGateDegraded === true);
  check('degraded_still_carries_digest', posted[0].digests.length === 1);

  /* ── 4. THE CRITICAL CASE: DEGRADED + EMPTY digest → STILL reports.
         A broken-bundle page usually has zero authoritative markers, because
         the missing reducer never wrote any. If the empty-digest early
         return still gated the POST, the signal would never fire on exactly
         the page this telemetry exists for. ── */
  window.resetIdentityGateWarnedForTests();
  posted = [];
  window.reportIdentityGateUnavailable('_onConvNotifyPush');
  await window.reportSyncDigest([]);
  await settle();
  check('degraded_empty_STILL_reports', posted.length === 1);
  check('degraded_empty_flag_set',
        posted.length === 1 && posted[0].extra &&
        posted[0].extra.identityGateDegraded === true);
  check('degraded_empty_digest_is_empty',
        posted.length === 1 && posted[0].digests.length === 0);

  /* ── 5. Telemetry only — reading the flag must not disturb the latch. ── */
  check('flag_read_is_idempotent',
        window.identityGateDegraded() === true &&
        window.identityGateDegraded() === true);

  report();
  process.exit(0);
})();
"""


def test_degraded_flag_rides_the_existing_digest():
    """Drive the REAL shipped reportSyncDigest: the flag travels on the
    existing probe, and a degraded gate reports even with an empty digest."""
    run_harness(
        target_js=_REDUCER,
        body_js=_BODY,
        min_pass=12,
        label='identity-gate degraded telemetry',
    )


def test_NEUTER_empty_digest_short_circuit_hides_the_signal():
    """NEUTER: restore the old ``if (!digests.length) return null`` gate →
    face 4 goes red. Proves the send-condition change (not the assertion) is
    what makes a broken-bundle page report at all."""
    import subprocess
    import tempfile

    from tests._jsdom import ROOT, node_deps_available

    if not node_deps_available():
        pytest.skip('node + jsdom dev-deps not installed')

    with open(_REDUCER, encoding='utf-8') as f:
        src = f.read()
    live = ('  const degraded = identityGateDegraded();\n'
            '  if (!digests.length && !degraded) return null;')
    assert live in src, 'send-condition anchor not found — did reportSyncDigest change?'
    neutered = src.replace(
        live,
        '  const degraded = identityGateDegraded();\n'
        '  if (!digests.length) return null;', 1)
    assert neutered != src

    tmp = []
    try:
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(_REDUCER), delete=False,
            encoding='utf-8',
        ) as fh:
            npath = fh.name
            fh.write(neutered)
        tmp.append(npath)
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(os.path.abspath(__file__)),
            delete=False, encoding='utf-8',
        ) as hf:
            harness = hf.name
            hf.write(_BODY)
        tmp.append(harness)
        proc = subprocess.run(
            ['node', harness, npath, ROOT],
            capture_output=True, text=True, timeout=60,
            env={**os.environ,
                 'JSDOM_HARNESS': os.path.join(
                     os.path.dirname(os.path.abspath(__file__)),
                     '_jsdom_harness.js')},
        )
        out = (proc.stdout or '').strip()
        assert 'FAIL degraded_empty_STILL_reports' in out, (
            'NEUTER did not bite — with the old empty-digest short circuit '
            'restored, a degraded gate on a marker-less page should go '
            f'unreported:\n{out}')
    finally:
        for p in tmp:
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


# ══════════════════════════════════════════════════════════════════════
#  Server — the flag is WARN-logged like every other drift signal
# ══════════════════════════════════════════════════════════════════════

def test_server_warns_on_degraded_flag(flask_client, caplog):
    """The endpoint must WARN-log the degrade with greppable context."""
    import logging
    caplog.set_level(logging.WARNING)
    resp = flask_client.post('/api/v1/conversations/sync-digest',
                             json={'digests': [],
                                   'identityGateDegraded': True})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    joined = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'IDENTITY GATE DEGRADED' in joined, (
        'degrade was not WARN-logged — it would stay invisible server-side:\n'
        + joined)
    assert 'SyncDrift' in joined, (
        'the degrade must carry the SyncDrift prefix so it greps alongside '
        'the divergence warnings it rides with')


def test_server_warns_even_with_empty_digests(flask_client, caplog):
    """The flag must be read BEFORE the digests validation. A broken-bundle
    page typically sends an empty list; validating first would swallow the
    one signal that matters."""
    import logging
    caplog.set_level(logging.WARNING)
    resp = flask_client.post('/api/v1/conversations/sync-digest',
                             json={'digests': [],
                                   'identityGateDegraded': True})
    assert resp.status_code == 200
    joined = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'IDENTITY GATE DEGRADED' in joined, joined


def test_server_silent_without_the_flag(flask_client, caplog):
    """A healthy client must not produce the degrade warning — otherwise the
    signal is noise and gets tuned out."""
    import logging
    caplog.set_level(logging.WARNING)
    resp = flask_client.post('/api/v1/conversations/sync-digest',
                             json={'digests': []})
    assert resp.status_code == 200
    joined = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'IDENTITY GATE DEGRADED' not in joined, joined


def test_flag_does_not_change_probe_semantics(flask_client):
    """Telemetry only: the flag must not alter the probe's response shape or
    its divergence computation."""
    import time
    conv_id = f'test-degraded-{time.time_ns()}'
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    now = int(time.time() * 1000)
    db.execute(
        'INSERT INTO conversations (id, user_id, title, messages, created_at, '
        'updated_at, settings, msg_count, search_text, rev) '
        'VALUES (?, 1, ?, ?, ?, ?, ?, 0, ?, ?)',
        (conv_id, 't', '[]', now, now, '{}', '', 5))
    db.commit()

    digest = [{'convId': conv_id, 'taskIds': ['ghost'], 'rev': 5}]
    without = flask_client.post('/api/v1/conversations/sync-digest',
                                json={'digests': digest}).get_json()
    with_flag = flask_client.post(
        '/api/v1/conversations/sync-digest',
        json={'digests': digest, 'identityGateDegraded': True}).get_json()
    assert without['checked'] == with_flag['checked']
    assert without['divergences'] == with_flag['divergences'], (
        'the telemetry flag changed the drift verdict — it must be inert')
