"""Cross-process wakeups are tied to committed jobs, never caller-local hints."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import uuid

import pytest

from lib.storage.errors import StorageError

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def test_watch_close_interrupts_only_its_owned_transport(semantic_authority):
    from lib.durable_worker.runtime_policy import runtime_policy
    from lib.storage.worker_wake import StorageWorkerWakeSource
    factory, _owner, _engine = semantic_authority
    waiting = threading.Event()
    class Client:
        def __init__(self, client):
            self.client = client
        def worker_watch(self, *, cursor, **kwargs):
            if cursor is not None:
                waiting.set()
            return self.client.worker_watch(cursor=cursor, **kwargs)
    source = StorageWorkerWakeSource(lambda: Client(factory()), runtime_policy())
    source.start(lambda operation: None)
    try:
        assert waiting.wait(5)
        with ThreadPoolExecutor() as executor:
            executor.submit(source.close).result(5)
        # Closing a subscription cannot close the shared client's other RPCs.
        assert factory().query('worker_job.claim_horizon', {
            'task_kinds': ['unrelated-control-test'], 'now_ms': int(time.time() * 1000),
        })['claimableInMs'] is None
    finally:
        source.close()


def test_committed_job_wakes_independent_client_and_gap_is_not_lost(semantic_authority):
    factory, owner, _engine = semantic_authority
    client = factory()
    before = client.worker_watch(cursor=None, deadline_unix_ms=int((time.time() + 5) * 1000))
    # The producer is a storage RPC; no application commit bus is subscribed.
    task = 'wake-' + uuid.uuid4().hex
    factory(write=True).command('worker_job.enqueue', {
        'task_id': task, 'user_id': owner, 'tenant_id': '', 'task_kind': 'wake-test',
        'payload': {}, 'idempotency_key': task, 'priority': 0,
        'available_at_ms': 0, 'now_ms': int(time.time() * 1000),
    }, command_id=uuid.uuid4().hex)
    after = client.worker_watch(cursor=before['cursor'], deadline_unix_ms=int((time.time() + 5) * 1000))
    assert after['cursor']['queue'] > before['cursor']['queue']
    assert after['cancelTasks'] == []
    factory(write=True).command('worker_job.request_cancel', {
        'task_id': task, 'user_id': owner, 'reason': 'Stop',
        'now_ms': int(time.time() * 1000),
    }, command_id=uuid.uuid4().hex)
    cancelled = client.worker_watch(cursor=after['cursor'], deadline_unix_ms=int((time.time() + 5) * 1000))
    assert cancelled['cursor']['cancel'] > after['cursor']['cancel']
    assert cancelled['cancelTasks'] is None  # Cancellation occurred between waits.


@pytest.mark.unit
def test_wait_holds_no_history_and_close_joins_waiters():
    from lib.storage_authority_sql.worker_control import WorkerControl
    control = WorkerControl()
    first = control.watch({'cursor': None}, time.monotonic() + 5)
    entered = threading.Event()

    def waiting():
        entered.set()
        return control.watch({'cursor': first['cursor']}, time.monotonic() + 5)

    with ThreadPoolExecutor() as pool:
        result = pool.submit(waiting)
        assert entered.wait(5)
        control.publish(False, {'one'})
        value = result.result(5)
        assert value['cancelTasks'] in (None, ['one'])
        pending = pool.submit(control.watch, {'cursor': value['cursor']}, time.monotonic() + 5)
        control.close()
        with pytest.raises(StorageError, match='closed'):
            pending.result(5)
    assert control._waiters == {}


@pytest.mark.unit
def test_worker_control_generation_is_current():
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    subprocess.run([sys.executable, 'scripts/gen_worker_control.py', '--check'], cwd=root, check=True)


@pytest.mark.unit
def test_authority_restart_refreshes_claims_even_without_cancel_history():
    from lib.storage.worker_wake import StorageWorkerWakeSource

    from lib.durable_worker.runtime_policy import runtime_policy
    source = StorageWorkerWakeSource(None, runtime_policy())
    active = threading.Event()
    unsubscribe = source.signals.subscribe('active', active)
    before = {'epoch': 'old', 'queue': 0, 'cancel': 0}
    after = {'epoch': 'new', 'queue': 0, 'cancel': 0}
    hints = []

    def watch(cursor):
        assert cursor == before
        source._stop.set()
        return {'cursor': after, 'cancelTasks': []}

    source._watch = watch
    source._run(before, hints.append)
    assert active.is_set()
    assert hints == ['authority-commit']
    unsubscribe()
    assert source.signals._active == {}


@pytest.mark.unit
def test_watch_interruptions_log_by_retry_class_and_keep_the_backoff_loop(caplog):
    """A stopping or restarting authority and an elapsed deadline are outlived
    by the next watch: DEBUG with the running count. A refused request, a
    protocol or engine fault and a client bug repeat on every watch until
    fixed: ERROR with the code. Each still resets the cursor, refreshes active
    claims and waits the failure backoff; a stop ends the loop silently."""
    import logging
    from lib.durable_worker.runtime_policy import runtime_policy
    from lib.storage.commit_wake import logger as wake_logger
    from lib.storage.worker_wake import StorageWorkerWakeSource

    class RecordingStop(threading.Event):
        def __init__(self):
            super().__init__()
            self.waits = []

        def wait(self, timeout=None):
            self.waits.append(timeout)
            return self.is_set()

    policy = runtime_policy()
    source = StorageWorkerWakeSource(None, policy)
    source._stop = RecordingStop()
    failures = [
        # A native control closed for a stop or restart (wire retryable).
        StorageError('database_unavailable', 'commit control closed', True, 50),
        # The SQL authority's closed control carries no retryable flag.
        StorageError('database_unavailable', 'Commit control closed'),
        # Clients map the wire deadline_elapsed to database_timeout.
        StorageError('database_timeout', 'commit watch deadline elapsed'),
        # Clients keep no worker_control_refused code: it arrives as database_internal.
        StorageError('worker_control_refused', 'commit watch binding differs'),
        StorageError('database_protocol_error', 'Invalid worker watch response'),
        TypeError("worker_watch() got an unexpected keyword argument 'cursor'"),
    ]
    cursors = []

    def watch(cursor):
        cursors.append(cursor)
        if failures:
            raise failures.pop(0)
        source._stop.set()
        raise StorageError('database_unavailable', 'commit watch cancelled', True)

    active = threading.Event()
    source.signals.subscribe('active', active)
    source._watch = watch
    caplog.set_level(logging.DEBUG, logger=wake_logger.name)
    initial = {'epoch': 'old', 'queue': 0, 'cancel': 0}
    source._run(initial, lambda operation: None)

    records = [(record.levelno, record.getMessage()) for record in caplog.records
               if record.name == wake_logger.name]
    assert [level for level, _message in records] == [logging.DEBUG] * 3 + [logging.ERROR] * 3
    for count, (_level, message) in enumerate(records[:3], start=1):
        assert f'retryable interruption {count}:' in message, message
    assert 'code=database_unavailable' in records[0][1]
    assert 'code=database_timeout' in records[2][1]
    assert "code=database_internal message='commit watch binding differs'" in records[3][1]
    assert 'code=database_protocol_error' in records[4][1]
    assert 'TypeError' in records[5][1]
    assert source.retryable_interruptions == 3
    # The loop itself is unchanged: every failure resets the cursor, refreshes
    # active claims and waits the existing failure backoff.
    assert cursors == [initial] + [None] * 6
    assert active.is_set()
    assert source._stop.waits == [policy['failureBackoffMilliseconds'] / 1000] * 6
