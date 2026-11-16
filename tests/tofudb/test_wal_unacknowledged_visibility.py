"""Readers see only acknowledged commits (executable specification; native only).

A commit's effect must not reach any reader before its WAL frame is acknowledged for the configured tier (Normal:
written; Strict: synced). Otherwise a process kill during a WAL stall erases a change a reader already saw: on
STORAGE-PRESSURE-COMPARE-1's kill schedule a ``turn.sync.changes`` head appeared 43 ms after its WAL write was held
and was gone after restart (.eval-runs/write-amp-20261113/results/BEFORE_AFTER_AE.md). Conversation sync reads use
the same published view as ``record.get``, which this specification observes. Design record:
docs/TOFUDB_WAL_BARRIER_SCHEDULING.md, "Visibility follows acknowledgement".

Uses the WAL write stall shim of ``test_wal_stall_liveness.py``; set TOFUDB_BIN to a source-matched engine.
"""
from __future__ import annotations

import sys
import threading
import time

import pytest

from tests.tofudb.test_wal_stall_liveness import (  # noqa: F401  (fixtures)
    _until, stalled_native_authority, stalled_native_engine,
)

pytestmark = [pytest.mark.api,
              pytest.mark.skipif(sys.platform != 'linux', reason='LD_PRELOAD write shim')]


def _start_stalled_put(client, trigger, results):
    def put_v2():
        try:
            results['v2'] = client.command(
                'record.put', {'namespace': 'visibility', 'key': 'k', 'value': {'v': 2}}, 'put-v2')
        except Exception as error:  # the crash case kills the engine under this command
            results['error'] = error

    writer = threading.Thread(target=put_v2)
    trigger.touch()
    writer.start()
    entered = trigger.with_name(trigger.name + '.entered')
    assert _until(time.monotonic() + 30, entered.exists), 'the v2 frame must reach the stalled WAL write'
    return writer


def _observe(client):
    observed = []
    for _ in range(20):
        observed.append(client.query('record.get', {'namespace': 'visibility', 'key': 'k'})['value'])
        time.sleep(0.01)
    return observed


def test_an_unacknowledged_commit_is_invisible_to_readers(stalled_native_authority):  # noqa: F811 (pytest fixture)
    client, _owner, trigger = stalled_native_authority
    client.command('record.put', {'namespace': 'visibility', 'key': 'k', 'value': {'v': 1}}, 'put-v1')
    results = {}
    try:
        writer = _start_stalled_put(client, trigger, results)
        observed = _observe(client)
        unacknowledged = client.metrics()['publishedReadView']['unacknowledgedViews']
        still_stalled = writer.is_alive()
    finally:
        trigger.unlink(missing_ok=True)
    writer.join(timeout=60)
    assert still_stalled, 'the v2 command answers only after its own WAL write'
    assert observed == [{'v': 1}] * len(observed), 'a reader saw a commit whose WAL frame was not acknowledged'
    assert unacknowledged >= 1, 'the v2 publication waits for its acknowledgement'
    assert results['v2']['version'] == 2
    assert client.query('record.get', {'namespace': 'visibility', 'key': 'k'})['value'] == {'v': 2}


def test_a_crash_during_a_wal_stall_never_erases_a_change_a_reader_saw(stalled_native_engine):  # noqa: F811
    engine, trigger = stalled_native_engine
    engine._client.command('record.put', {'namespace': 'visibility', 'key': 'k', 'value': {'v': 1}}, 'put-v1')
    results = {}
    try:
        writer = _start_stalled_put(engine._client, trigger, results)
        observed = _observe(engine._client)
        assert writer.is_alive(), 'the v2 command must still wait for its stalled WAL write'
        # Process loss while the v2 frame is still unwritten.
        engine._process.kill()
        engine._process.wait(timeout=60)
    finally:
        trigger.unlink(missing_ok=True)
    writer.join(timeout=60)
    assert 'v2' not in results, 'the killed engine acknowledged v2'
    engine._launch_serve()
    recovered = engine._client.query('record.get', {'namespace': 'visibility', 'key': 'k'})['value']
    assert recovered == {'v': 1}, 'the stalled, unacknowledged v2 frame cannot survive the kill'
    lost = [value for value in observed if value != recovered]
    assert not lost, f'readers saw {lost}, which the crash erased'
