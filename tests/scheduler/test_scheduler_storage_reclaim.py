"""Scheduler coverage for the native store's built-in Storage Reclaim task.

The task is the owner's switch for the engine's online collectors
(contracts/storage_v2.json ``nativeOnlineGc``): the engine pauses them while
the task is disabled. Its daily run only rings the collector and records
what it has freed.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


class _Configuration:
    def __init__(self, backend: str):
        self.storage_backend = backend


@pytest.mark.parametrize('backend, registered', [('tofudb', True), ('sqlite', False)])
def test_reclaim_task_registers_with_the_contract_key_on_native_storage_only(
        monkeypatch, backend, registered):
    import runtime_guards
    import lib.scheduler.manager as manager_module

    monkeypatch.setattr(
        runtime_guards, 'load_deployment_configuration',
        lambda *_args, **_kwargs: _Configuration(backend))
    manager = manager_module.ScheduledTaskManager.__new__(
        manager_module.ScheduledTaskManager)
    captured = []
    monkeypatch.setattr(
        manager,
        '_ensure_default_task',
        lambda **kwargs: (captured.append(kwargs) or ({'id': 'reclaim'}, False)),
    )

    manager._ensure_default_storage_reclaim_task()

    if not registered:
        assert captured == [], 'only native storage has collectors to pause'
        return
    from lib.storage.generated_v2_semantics import (
        NATIVE_ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY, NATIVE_ONLINE_GC_SCHEDULER_SYSTEM_KEY)

    [task] = captured
    assert task['system_key'] == NATIVE_ONLINE_GC_SCHEDULER_SYSTEM_KEY == 'storage-reclaim'
    assert task['system_key'] != NATIVE_ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY
    assert (task['task_type'], task['command']) == ('storage_reclaim', 'storage.system.reclaim')
    assert task.get('enabled', True) is True
    assert 'Turn this task off to pause' in task['description']


def test_reclaim_run_rings_the_collector_and_records_its_totals(monkeypatch):
    import lib.scheduler.manager as manager_module

    calls = []

    class _Client:
        def command(self, operation, payload, command_id, priority='user', deadline=None):
            calls.append((operation, dict(payload), command_id, priority, deadline))
            return {'backend': 'tofu-db', 'reclaimed': 1200, 'reclaimed_bytes': 3 * 1024 * 1024,
                    'epochs_completed': 4, 'epoch_in_progress': True}

    monkeypatch.setattr(manager_module, '_scheduler_client', lambda **_kwargs: _Client())
    manager = manager_module.ScheduledTaskManager.__new__(
        manager_module.ScheduledTaskManager)

    ok, message = manager._execute_task({
        'id': 'reclaim-task', 'user_id': 1, 'task_type': 'storage_reclaim',
        'command': 'storage.system.reclaim', 'max_runtime': 300,
    })

    assert ok is True
    assert calls == [('system.reclaim', {}, None, 'maintenance', 300.0)]
    assert '1200 blocks (3.0 MiB)' in message and 'over 4 passes' in message
    assert 'a pass is running' in message


def test_a_failed_reclaim_run_names_the_storage_error(monkeypatch):
    import lib.scheduler.manager as manager_module

    class _Client:
        def command(self, *_args, **_kwargs):
            raise RuntimeError('database_unavailable: storage_full')

    monkeypatch.setattr(manager_module, '_scheduler_client', lambda **_kwargs: _Client())
    manager = manager_module.ScheduledTaskManager.__new__(
        manager_module.ScheduledTaskManager)

    ok, message = manager._execute_task({
        'id': 'reclaim-task', 'user_id': 1, 'task_type': 'storage_reclaim',
        'command': 'storage.system.reclaim', 'max_runtime': 300,
    })

    assert ok is False
    assert message == 'Storage reclaim error: database_unavailable: storage_full'
