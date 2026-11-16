"""Readers see only acknowledged commits (executable specification; native only).

A commit's effect must not reach any reader before its WAL frame is acknowledged for the configured tier (Normal:
written; Strict: synced). Otherwise a process kill during a WAL stall erases a change a reader already saw: on
STORAGE-PRESSURE-COMPARE-1's kill schedule a ``turn.sync.changes`` head appeared 43 ms after its WAL write was held
and was gone after restart (.eval-runs/write-amp-20261113/results/BEFORE_AFTER_AE.md). Conversation sync reads use
the same published view as ``record.get``, which this specification observes.

Uses the WAL write stall shim of ``test_wal_stall_liveness.py``; set TOFUDB_BIN to a source-matched engine.
"""
from __future__ import annotations

import sys
import threading
import time

import pytest

from tests.tofudb.test_wal_stall_liveness import (  # noqa: F401  (fixture)
    _until, stalled_native_authority,
)

pytestmark = [pytest.mark.api,
              pytest.mark.skipif(sys.platform != 'linux', reason='LD_PRELOAD write shim')]


@pytest.mark.xfail(strict=True, reason=(
    'Open engine defect (failing-first spec from spec/wal-unacknowledged-visibility-20261113): the published '
    'view exposes a commit before its WAL frame is acknowledged. No recovered line carries a fix.'))
def test_an_unacknowledged_commit_is_invisible_to_readers(stalled_native_authority):  # noqa: F811 (pytest fixture)
    client, _owner, trigger = stalled_native_authority
    client.command('record.put', {'namespace': 'visibility', 'key': 'k', 'value': {'v': 1}}, 'put-v1')
    results = {}

    def put_v2():
        results['v2'] = client.command(
            'record.put', {'namespace': 'visibility', 'key': 'k', 'value': {'v': 2}}, 'put-v2')

    writer = threading.Thread(target=put_v2)
    trigger.touch()
    try:
        writer.start()
        entered = trigger.with_name(trigger.name + '.entered')
        assert _until(time.monotonic() + 30, entered.exists), 'the v2 frame must reach the stalled WAL write'
        observed = []
        for _ in range(20):
            observed.append(client.query('record.get', {'namespace': 'visibility', 'key': 'k'})['value'])
            time.sleep(0.01)
        still_stalled = writer.is_alive()
    finally:
        trigger.unlink(missing_ok=True)
    writer.join(timeout=60)
    assert still_stalled, 'the v2 command answers only after its own WAL write'
    assert observed == [{'v': 1}] * len(observed), 'a reader saw a commit whose WAL frame was not acknowledged'
    assert client.query('record.get', {'namespace': 'visibility', 'key': 'k'})['value'] == {'v': 2}
