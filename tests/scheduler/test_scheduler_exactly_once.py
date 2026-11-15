"""A scheduled occurrence fires once across a restart and two concurrent claimers.

``scheduler.task.claim_due`` takes the row lock, refuses a disabled row and
claims at most once per ``DUE_CLAIM_INTERVAL_SECONDS`` on both storage
authorities (docs/modules/scheduling_ops.md, "Governed scheduling"). Two
manager-side rules keep that guarantee end to end:

* a ``once:`` row whose claim column is set never fires again, so a process
  that stopped between its claim and its disable cannot fire it twice;
* a claim's command id names its claimant, because storage replays a repeated
  command id as the original result: two processes claiming at the same
  instant under one shared id would both read ``claimed``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import threading
import uuid

import pytest

import lib.scheduler.manager as manager_module
from lib.scheduler.manager import ScheduledTaskManager
from tests.tofudb.test_tofudb_differential import tofudb_binary, tofudb_daemon  # noqa: F401 (fixtures)

# serial: the native case builds/spawns the tofu-db engine (TESTING_STRATEGY).
pytestmark = [pytest.mark.unit, pytest.mark.serial]

_OWNER = 11  # the owner tests/tofudb/test_tofudb_differential.py authenticates


class _Authority:
    """The storage client port over either authority, raising on a failed operation."""

    def __init__(self, execute):
        self._execute = execute

    def query(self, operation, payload, **_kwargs):
        return self._execute(operation, payload, None)

    def command(self, operation, payload, key=None, **_kwargs):
        return self._execute(operation, payload, key or uuid.uuid4().hex)


@pytest.fixture(params=['sqlite', 'tofudb'])
def authority(request, tmp_path, monkeypatch):
    if request.param == 'tofudb':
        driver = request.getfixturevalue('tofudb_daemon')
        # The harness drives one storage.v2 connection, which carries one frame at a
        # time; the claimers still race request by request at the authority.
        connection = threading.Lock()

        def execute(operation, payload, key):
            with connection:
                outcome = driver.execute(operation, payload, key)
            if not outcome.ok:
                raise RuntimeError(f'{operation}: {outcome.error_code} {outcome.error_message}')
            return outcome.value
    else:
        from lib.storage import StorageSupervisor
        supervisor = StorageSupervisor(project_root=tmp_path, backend='sqlite', startup_timeout=60)
        supervisor.start()
        request.addfinalizer(supervisor.stop)

        def execute(operation, payload, key):
            return (supervisor.client.query(operation, payload) if key is None
                    else supervisor.client.command(operation, payload, key))
    client = _Authority(execute)
    monkeypatch.setattr(manager_module, '_scheduler_client', lambda *, write=False: client)
    return client


class _Clock(datetime):
    current = datetime(2026, 11, 10, 22, 0, 5)

    @classmethod
    def now(cls, tz=None):
        return cls.current


def _due_row(client, task_id, schedule):
    client.command('scheduler.task.create', {
        'task_id': task_id, 'user_id': _OWNER, 'name': task_id, 'schedule': schedule,
        'task_type': 'prompt', 'command': 'Summarize the day.', 'created_at': '2026-11-10T21:00:00',
        'updated_at': '2026-11-10T21:00:00'}, f'create-{task_id}')


def _counting_manager(monkeypatch, runs):
    manager = ScheduledTaskManager()

    def execute(task):
        runs.append(task['id'])
        return True, 'ran'
    monkeypatch.setattr(manager, '_execute_task', execute)
    return manager


def test_a_one_time_row_claimed_before_a_restart_never_fires_again(authority, monkeypatch):
    monkeypatch.setattr(manager_module, 'datetime', _Clock)
    _due_row(authority, 'once-row', 'once:2026-11-10 22:00')
    runs = []
    stopped = _counting_manager(monkeypatch, runs)

    def stop_after_claim(task):  # the process dies after its claim committed, before it ran or disabled the row
        raise RuntimeError('server stopped')
    monkeypatch.setattr(stopped, '_dispatch_claimed_task', stop_after_claim)
    _Clock.current = datetime(2026, 11, 10, 22, 0, 5)
    stopped._check_and_run_due_tasks()
    claimed = authority.query('scheduler.task.get', {'task_id': 'once-row', 'user_id': _OWNER})
    assert claimed['last_run'] and claimed['enabled'] and claimed['run_count'] == 0

    restarted = _counting_manager(monkeypatch, runs)
    for seconds in (70, 200):
        _Clock.current = datetime(2026, 11, 10, 22, 0, 5) + timedelta(seconds=seconds)
        restarted._check_and_run_due_tasks()
    assert restarted.stop(timeout=10)
    assert runs == []


def test_two_concurrent_claimers_fire_each_due_occurrence_once(authority, monkeypatch):
    monkeypatch.setattr(manager_module, 'datetime', _Clock)
    _Clock.current = datetime(2026, 11, 10, 22, 0, 5)
    _due_row(authority, 'cron-row', '* * * * *')
    _due_row(authority, 'once-due', 'once:2026-11-10 22:00')
    runs = []
    managers = [_counting_manager(monkeypatch, runs) for _ in range(2)]
    start = threading.Barrier(2)
    errors = []

    def tick(manager):
        try:
            start.wait(10)
            manager._check_and_run_due_tasks()
        except Exception as error:
            errors.append(error)
    threads = [threading.Thread(target=tick, args=(manager,)) for manager in managers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    for manager in managers:
        assert manager.stop(timeout=10)

    assert errors == []
    assert sorted(runs) == ['cron-row', 'once-due']
