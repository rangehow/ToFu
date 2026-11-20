"""Private SQLite service/runtime replacement reuses the no-server successor trace.

Only storage.v1 lifecycle and synthetic conversation recovery are qualified;
no production application, native image or automatic restart is admitted.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conversation.test_coordinator_session_control import tool_checkpoint_session as tool_checkpoint_session
from tests.conversation.test_checkpoint_exit_successor import _assert_exited_checkpoint_successor

pytestmark = pytest.mark.api


@pytest.fixture
def storage(tmp_path, monkeypatch, record_property):
    from lib import storage as storage_module, turn_lifecycle as turns
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.errors import StorageError
    from tests.support.scratch_server import prepare_scratch_environment

    environment = prepare_scratch_environment(tmp_path, 9, Path(__file__).resolve().parents[2], {})
    for name in tuple(os.environ):
        if name.startswith('TOFU_'):
            monkeypatch.delenv(name)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    slot = SimpleNamespace(config=None, generations=[], stages=[])

    def start():
        supervisor = StorageSupervisor(project_root=tmp_path, backend='sqlite',
            connection_file='', sqlite_fastpath_mode='off')
        runtime = StorageRuntime(supervisor, auto_restart=False)
        slot.runtime = runtime
        slot.client = runtime.start()
        slot.endpoint = (*slot.client.endpoint, supervisor._token)
        health = slot.client.health()
        assert health['backend'] == 'sqlite'
        assert runtime.status()['ready'] is True
        process = supervisor._process
        assert process is not None and process.poll() is None
        slot.generations.append(process.pid)
        slot.stages.append('sqlite-service-ready')
        return process

    def restart():
        old_client, old_runtime = slot.client, slot.runtime
        old_process = slot.process
        slot.stages.append('source-checkpoint-committed-and-child-exited')
        old_runtime.stop()
        assert old_process.poll() is not None
        assert old_runtime.status()['ready'] is False
        slot.stages.append('owned-service-joined-and-transport-closed')
        with pytest.raises(StorageError) as refusal:
            old_client.query('turn.get', {'conversation_id': 'session', 'turn_id': 'absent', 'user_id': 1})
        assert refusal.value.code == 'database_unavailable'
        with pytest.raises(StorageError):
            old_runtime.client(write=True)
        slot.stages.append('retired-transport-and-runtime-refused')
        slot.process = start()
        assert slot.process.pid != old_process.pid
        assert slot.client is not old_client
        slot.stages.append('fresh-runtime-reconnected-to-retained-sqlite')

    slot.restart = restart
    monkeypatch.setattr(turns, '_turn_client', lambda **kw: slot.client)
    monkeypatch.setattr(storage_module, 'get_storage_client', lambda **kw: slot.client)
    try:
        slot.process = start()
        yield slot
    finally:
        slot.runtime.stop()
        assert slot.process.poll() is not None
        assert slot.runtime.status()['ready'] is False
        record_property('sqlite_service_lifecycle', json.dumps(dict(
            pids=slot.generations, stages=slot.stages, automaticRestart=False,
            scope='private storage.v1 runtime replacement; not full application/native parity')))


def test_service_restart_rehydrates_checkpoint_and_authorized_successor(
        tool_checkpoint_session, monkeypatch, tmp_path, record_property):
    _assert_exited_checkpoint_successor(
        tool_checkpoint_session, monkeypatch, tmp_path, record_property)
    assert tool_checkpoint_session.storage.stages == [
        'sqlite-service-ready', 'source-checkpoint-committed-and-child-exited',
        'owned-service-joined-and-transport-closed', 'retired-transport-and-runtime-refused',
        'sqlite-service-ready', 'fresh-runtime-reconnected-to-retained-sqlite']
