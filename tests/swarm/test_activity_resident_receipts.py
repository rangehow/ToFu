"""The background-activity snapshot answers from each live session's resident receipts.

``GET /api/v4/activity/background`` reaches the resident execution host through
the private execution control, whose answer must arrive within the contract's
acknowledgement (``contracts/command_control_v1.json``). The execution host
answers from the receipts its live swarm sessions already hold: every mailbox
commit of the process applies to them before it returns, and the reads the
session performs anyway (roster snapshots, and the refresh each activity read
requests) replace what another process can change. No storage read happens
while the request waits, so storage latency cannot turn the poll into a 503,
and the answer is the same data a storage read of the session returns.

Decision: "Background-activity receipts" in docs/modules/task_engine.md.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from lib.storage.errors import StorageError
from lib.swarm import message_receipts
from lib.swarm.integration import _state
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubAgentResult, SubTaskSpec
from lib.swarm.run_messages import RunMessageRepository, binding_for
from lib.swarm.team_messages import coordinator_binding
from tests.swarm.test_retained_agents import RecordClient
from tests.swarm.test_swarm_execution_controls import (  # noqa: F401
    ROOT, _install_execution_owner, _line_within, api_app,
)
__all__ = ['api_app']

pytestmark = pytest.mark.unit
OWNER, CONV, RUN = 7, 'conv-a', 'launch'
RECEIPT_FIELDS = ('messageReceipts', 'messageReceiptsVersions',
                  'messageReceiptsAvailable', 'messageParticipants')

_SLOW_EXECUTION_HOST = f'''
import asyncio, copy, json, sys, time
import lib.storage
import lib.storage.service
from lib.storage.errors import StorageError
from lib.swarm.integration import _active_sessions, _session_timestamps, _sessions_lock
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubTaskSpec
from lib.swarm.run_messages import RunMessageRepository, binding_for
from lib.tasks_pkg.command_control_transport import CommandControlServer
from lib.tasks_pkg.command_controls import apply_command_control
from lib.tasks_pkg.generated_command_control import ACKNOWLEDGEMENT_SECONDS

OWNER, CONV, RUN = {OWNER!r}, {CONV!r}, {RUN!r}


class Storage:
    """One conversation and its mailboxes; once slow, every read outlasts the acknowledgement."""

    def __init__(self):
        self.rows, self.slow = {{}}, False

    def query(self, operation, payload):
        if self.slow:
            time.sleep(ACKNOWLEDGEMENT_SECONDS + 0.5)
        if operation == 'conversation.get':
            if (payload['conv_id'], payload['user_id']) != (CONV, OWNER):
                return None
            return {{'metadata': {{'settings': {{'projectPath': '/p'}}}}, 'messages': []}}
        if operation != 'record.get':
            raise StorageError('database_unavailable', 'outside this fixture')
        return copy.deepcopy(self.rows.get((payload['namespace'], payload['key'])))

    def command(self, operation, payload, command_id):
        if operation != 'record.put':
            raise StorageError('database_unavailable', 'outside this fixture')
        key = (payload['namespace'], payload['key'])
        actual = self.rows[key]['version'] if key in self.rows else 0
        if payload.get('expected_version') != actual:
            raise StorageError('database_conflict', 'stale record')
        self.rows[key] = {{'value': copy.deepcopy(payload['value']), 'version': actual + 1}}
        return {{'version': actual + 1}}


async def main():
    storage = Storage()
    # Every storage read of this process, whichever entry point resolves the client.
    lib.storage.get_storage_client = lib.storage.service.get_storage_client = (
        lambda write=False: storage)
    task = {{'id': RUN, 'convId': CONV, '_userId': OWNER, 'config': {{'projectPath': '/p'}}}}
    spec = SubTaskSpec(id='child-a', role='coder', objective='Inspect')
    # A spawn passes its parent task's model, which also names pending workers.
    session = MasterOrchestrator(RUN, CONV, [spec], user_id=OWNER, model='parent-model')
    session._parent_task_proxy, session.inbox_key = task, CONV
    with _sessions_lock:
        _active_sessions[CONV] = session
        _session_timestamps[CONV] = time.time()
    # At normal latency: one real commit and the roster snapshot it refreshes.
    RunMessageRepository(binding_for(task, spec, CONV, run_id=RUN)).submit(
        content='Evidence committed before storage slowed', kind='reference',
        key='before-slow', sender_task_id=RUN)
    storage.slow = True
    server = CommandControlServer('incarnation', apply_command_control)
    print(json.dumps(await server.start()), flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    await server.close()
    print(json.dumps({{'closed': True}}), flush=True)

asyncio.run(main())
'''


_SHARED_FILE_STORAGE = '''
import copy, fcntl, json, os
import lib.storage
import lib.storage.service
from lib.storage.errors import StorageError


class FileStorage:
    """One conversation and the mailboxes, in a JSON file every process locks."""

    def __init__(self, path):
        self.path = path

    def _locked(self, change=None):
        with open(self.path, 'a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            text = handle.read()
            rows = json.loads(text) if text else {}
            if change is None:
                return rows
            result = change(rows)
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(rows))
            handle.flush()
            os.fsync(handle.fileno())
            return result

    def query(self, operation, payload):
        if operation == 'conversation.get':
            if (payload['conv_id'], payload['user_id']) != (CONV, OWNER):
                return None
            return {'metadata': {'settings': {'projectPath': '/p'}}, 'messages': []}
        if operation != 'record.get':
            raise StorageError('database_unavailable', 'outside this fixture')
        return copy.deepcopy(self._locked().get(payload['namespace'] + '/' + payload['key']))

    def command(self, operation, payload, command_id):
        if operation != 'record.put':
            raise StorageError('database_unavailable', 'outside this fixture')
        def put(rows):
            key = payload['namespace'] + '/' + payload['key']
            actual = rows[key]['version'] if key in rows else 0
            if payload.get('expected_version') != actual:
                raise StorageError('database_conflict', 'stale record')
            rows[key] = {'value': payload['value'], 'version': actual + 1}
            return {'version': actual + 1}
        return self._locked(put)


def install(path):
    storage = FileStorage(path)
    lib.storage.get_storage_client = lib.storage.service.get_storage_client = (
        lambda write=False: storage)
'''

_SHARED_PREAMBLE = f'''
import asyncio, json, sys, time
OWNER, CONV, RUN = {OWNER!r}, {CONV!r}, {RUN!r}
TASK = {{'id': RUN, 'convId': CONV, '_userId': OWNER, 'config': {{'projectPath': '/p'}}}}
''' + _SHARED_FILE_STORAGE

_SHARED_EXECUTION_HOST = _SHARED_PREAMBLE + '''
from lib.swarm.integration import _active_sessions, _session_timestamps, _sessions_lock
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubTaskSpec
from lib.swarm.run_messages import RunMessageRepository, binding_for
from lib.tasks_pkg.command_control_transport import CommandControlServer
from lib.tasks_pkg.command_controls import apply_command_control


async def main():
    install(sys.argv[1])
    spec = SubTaskSpec(id='child-a', role='coder', objective='Inspect')
    session = MasterOrchestrator(RUN, CONV, [spec], user_id=OWNER, model='parent-model')
    session._parent_task_proxy, session.inbox_key = TASK, CONV
    with _sessions_lock:
        _active_sessions[CONV] = session
        _session_timestamps[CONV] = time.time()
    RunMessageRepository(binding_for(TASK, spec, CONV, run_id=RUN)).submit(
        content='Held by the live session', kind='reference', key='local',
        sender_task_id=RUN)
    server = CommandControlServer('incarnation', apply_command_control)
    print(json.dumps(await server.start()), flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    await server.close()
    print(json.dumps({'closed': True}), flush=True)

asyncio.run(main())
'''

# Another process (an execution shard, a retained run) commits to the same
# mailbox; it holds no live session, so no commit hook of the host sees it.
_OTHER_PROCESS_WRITER = _SHARED_PREAMBLE + '''
from lib.swarm.protocol import SubTaskSpec
from lib.swarm.run_messages import RunMessageRepository, binding_for

install(sys.argv[1])
spec = SubTaskSpec(id='child-a', role='coder', objective='Inspect')
sent = RunMessageRepository(binding_for(TASK, spec, CONV, run_id=RUN)).submit(
    content='Written by another process', kind='reference', key='remote',
    sender_task_id=RUN)
print(json.dumps({'id': sent['id']}), flush=True)
'''


@pytest.fixture
def shared_execution_host(tmp_path):
    storage = tmp_path / 'storage.json'
    process = subprocess.Popen(
        [sys.executable, '-c', _SHARED_EXECUTION_HOST, str(storage)], cwd=ROOT, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={**os.environ, 'PYTHONPATH': str(ROOT)})
    try:
        endpoint = _line_within(process.stdout, 120)

        def finish():
            process.stdin.close()
            return _line_within(process.stdout, 60)

        yield endpoint, storage, finish
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_another_process_write_reaches_a_following_poll_never_an_empty_list(
        shared_execution_host, api_app):
    endpoint, storage, finish = shared_execution_host
    _install_execution_owner(api_app, endpoint)

    async def poll():
        response = await api_app.test_client().get('/api/v4/activity/background')
        assert response.status_code == 200, await response.get_data()
        [swarm] = (await response.get_json())['data']['swarms']
        return swarm

    def contents(swarm):
        # Every answer holds what the session already read: unavailable or
        # partial is never shown as an empty mailbox.
        assert swarm['messageReceiptsAvailable'] is True, swarm
        found = [row['content'] for row in swarm['messageReceipts']]
        assert 'Held by the live session' in found, found
        return found

    assert contents(asyncio.run(poll())) == ['Held by the live session']
    written = subprocess.run(
        [sys.executable, '-c', _OTHER_PROCESS_WRITER, str(storage)], cwd=ROOT, text=True,
        capture_output=True, timeout=120, env={**os.environ, 'PYTHONPATH': str(ROOT)})
    assert written.returncode == 0, written.stderr[-2000:]
    remote = json.loads(written.stdout.strip().splitlines()[-1])['id']
    deadline = time.monotonic() + 30
    while True:
        swarm = asyncio.run(poll())
        found = contents(swarm)
        if remote in {row['messageId'] for row in swarm['messageReceipts']}:
            break
        assert time.monotonic() < deadline, 'the other process\'s write never reached a poll'
        time.sleep(0.05)
    assert sorted(found) == ['Held by the live session', 'Written by another process']
    assert finish() == {'closed': True}


@pytest.fixture
def slow_execution_host():
    process = subprocess.Popen(
        [sys.executable, '-c', _SLOW_EXECUTION_HOST], cwd=ROOT, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={**os.environ, 'PYTHONPATH': str(ROOT)})
    try:
        endpoint = _line_within(process.stdout, 120)

        def finish():
            process.stdin.close()
            return _line_within(process.stdout, 60)

        yield endpoint, finish
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_activity_answers_while_storage_reads_outlast_the_acknowledgement(
        slow_execution_host, api_app):
    endpoint, finish = slow_execution_host
    _install_execution_owner(api_app, endpoint)

    async def exercise():
        response = await api_app.test_client().get('/api/v4/activity/background')
        return response.status_code, await response.get_json()

    status, body = asyncio.run(exercise())
    assert status == 200, body
    [swarm] = body['data']['swarms']
    assert (swarm['swarm_id'], swarm['conv_id']) == (RUN, CONV)
    assert swarm['messageReceiptsAvailable'] is True
    assert [row['content'] for row in swarm['messageReceipts']] == [
        'Evidence committed before storage slowed']
    assert finish() == {'closed': True}


def _join_refreshes():
    for thread in threading.enumerate():
        if thread.name == 'swarm-receipts-refresh':
            thread.join(10)




@pytest.fixture
def team(monkeypatch):
    client = RecordClient()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda write=False: client)
    monkeypatch.setattr('lib.storage.service.get_storage_client', lambda write=False: client)
    settings = {'projectPath': '/p'}
    scope_reads = []

    def conversation(_self, conv_id, *, user_id, include_messages):
        scope_reads.append(threading.get_ident())
        if (conv_id, user_id) != (CONV, OWNER):
            return None
        return SimpleNamespace(metadata={'settings': dict(settings)})

    monkeypatch.setattr('lib.conversations.repository.ConversationRepository.get', conversation)
    # Roster snapshots still refresh on every commit; only their turn write is out of scope.
    monkeypatch.setattr('lib.swarm.snapshot.persist_snapshot_to_conversation',
                        lambda *args, **kwargs: None)
    task = {'id': RUN, 'convId': CONV, '_userId': OWNER, 'config': {'projectPath': '/p'}}
    specs = [SubTaskSpec(id='child-a', role='coder', objective='Inspect'),
             SubTaskSpec(id='child-b', role='researcher', objective='Survey')]
    master = MasterOrchestrator(RUN, CONV, specs, user_id=OWNER, model='parent-model')
    master._parent_task_proxy, master.inbox_key = task, CONV
    monkeypatch.setattr(_state, '_active_sessions', {CONV: master})
    monkeypatch.setattr(_state, '_session_timestamps', {CONV: time.time()})
    bindings = {spec.id: binding_for(task, spec, CONV, run_id=RUN) for spec in specs}
    yield SimpleNamespace(client=client, settings=settings, scope_reads=scope_reads,
                          master=master, specs=specs, bindings=bindings)
    _join_refreshes()


def _todays_projection(master):
    """The storage-read projection of the same live session: today's answer."""
    agents = [{'id': agent_id, **info} for agent_id, info in master.get_status().items()]
    with master._lock:
        specs = list(master.specs)
    return message_receipts.snapshot_receipts(master, agents, specs), agents


def _assert_same_as_today(row, master):
    expected, agents = _todays_projection(master)
    assert {field: row[field] for field in RECEIPT_FIELDS} == expected
    assert row['agents'] == agents


def _activity():
    [row] = _state.list_active_swarms(user_id=OWNER)
    return row


def _activity_until(available):
    """Poll as the dock does until an answer reports this availability."""
    deadline = time.monotonic() + 10
    while True:
        row = _activity()
        if row['messageReceiptsAvailable'] is available:
            return row
        assert time.monotonic() < deadline, 'the activity answer never reflected the change'
        time.sleep(0.01)


def _seed(team):
    return RunMessageRepository(team.bindings['child-a']).submit(
        content='Check the parser\n— exact 雪🙂', kind='steer', key='k1', sender_task_id=RUN)


def test_activity_answers_the_same_receipts_as_a_storage_read(team):
    master = team.master
    to_a = RunMessageRepository(team.bindings['child-a'])
    sent = _seed(team)
    to_a.record_injected([sent['id']], child_task_id='runtime-child-1', round_num=0)
    report = RunMessageRepository(coordinator_binding(master)).submit(
        content='Worker report', kind='reference', key='r1', sender_task_id='runtime-child-1')
    survey = RunMessageRepository(team.bindings['child-b']).submit(
        content='Survey the docs', kind='reference', key='k2', sender_task_id=RUN)
    # child-a is live (its invocation witnesses the participant); child-b failed
    # with its input still submitted, which the projection reports unconfirmed.
    master._agents['child-a'] = SimpleNamespace(result=None, _child={
        'id': 'runtime-child-1', '_userId': OWNER, 'config': {},
        '_swarmMessageBinding': team.bindings['child-a']})
    with master._lock:
        master._results_by_id['child-b'] = (
            team.specs[1], SubAgentResult(status='failed', error_message='boom'))

    row = _activity()
    assert row['messageReceiptsAvailable'] is True
    assert {receipt['messageId'] for receipt in row['messageReceipts']} == {
        sent['id'], report['id'], survey['id']}
    _assert_same_as_today(row, master)

    # A later commit is already in the next answer, without another read.
    to_a.confirm([sent['id']], child_task_id='runtime-child-1', round_num=1)
    row = _activity()
    [confirmed] = [receipt for receipt in row['messageReceipts']
                   if receipt['messageId'] == sent['id']]
    assert confirmed['state'] == 'input_confirmed'
    _assert_same_as_today(row, master)


def test_activity_read_never_waits_for_storage(team, monkeypatch):
    _seed(team)
    reader = threading.get_ident()
    query = team.client.query
    reads = []

    def counted(operation, payload):
        if threading.get_ident() == reader:
            reads.append(operation)
        return query(operation, payload)

    monkeypatch.setattr(team.client, 'query', counted)
    team.scope_reads.clear()
    row = _activity()
    assert row['messageReceiptsAvailable'] is True
    assert reads == []
    assert reader not in team.scope_reads


def test_a_commit_is_in_every_answer_that_starts_after_it_returns(team, monkeypatch):
    _seed(team)
    _activity()
    _join_refreshes()
    # Without the roster snapshot's re-read, the commit itself must be resident.
    monkeypatch.setattr('lib.swarm.message_receipts.persist_receipt_snapshot', lambda binding: None)
    later = RunMessageRepository(team.bindings['child-b']).submit(
        content='Second request', kind='reference', key='k2', sender_task_id=RUN)
    row = _activity()
    assert later['id'] in {receipt['messageId'] for receipt in row['messageReceipts']}
    _assert_same_as_today(row, team.master)


def test_a_scope_change_from_another_process_reaches_the_following_answers(team):
    _seed(team)
    assert _activity()['messageReceiptsAvailable'] is True
    team.settings['archived'] = True  # e.g. the API host archived the conversation
    row = _activity_until(False)
    assert row['messageReceipts'] == []
    _assert_same_as_today(row, team.master)
    del team.settings['archived']
    _assert_same_as_today(_activity_until(True), team.master)


def test_receipts_the_session_has_not_read_are_unavailable_never_empty(team):
    # Registered, yet no commit or roster snapshot has read its mailboxes.
    row = _activity()
    assert row['messageReceiptsAvailable'] is False
    assert row['messageReceipts'] == [] and row['messageReceiptsVersions'] == {}
    # That answer asked the session to read them; the following answers carry them.
    _assert_same_as_today(_activity_until(True), team.master)


def test_an_unconfirmed_mailbox_write_is_reread_never_assumed(team, monkeypatch):
    _seed(team)
    _activity()
    _join_refreshes()
    command = team.client.command

    def lost(operation, payload, command_id):
        command(operation, payload, command_id)
        raise StorageError('database_unavailable', 'connection reset after send')

    monkeypatch.setattr(team.client, 'command', lost)
    with pytest.raises(StorageError):
        RunMessageRepository(team.bindings['child-a']).submit(
            content='Outcome unknown to the writer', kind='reference', key='k3',
            sender_task_id=RUN)
    monkeypatch.setattr(team.client, 'command', command)
    row = _activity_until(True)
    assert 'Outcome unknown to the writer' in {
        receipt['content'] for receipt in row['messageReceipts']}
    _assert_same_as_today(row, team.master)


def test_a_mailbox_write_from_another_process_reaches_the_following_answers(team, monkeypatch):
    # Execution shards and retained runs can commit to a mailbox this session
    # already holds; no commit hook of this process sees that write.
    _seed(team)
    assert _activity()['messageReceiptsAvailable'] is True
    _join_refreshes()
    with monkeypatch.context() as elsewhere:
        elsewhere.setattr('lib.swarm.message_receipts.hold_committed_mailbox', lambda *a: None,
                            raising=False)
        elsewhere.setattr('lib.swarm.message_receipts.persist_receipt_snapshot', lambda *a: None)
        remote = RunMessageRepository(team.bindings['child-a']).submit(
            content='Written by another process', kind='reference', key='k9',
            sender_task_id=RUN)
    deadline = time.monotonic() + 10
    while remote['id'] not in {receipt['messageId'] for receipt in _activity()['messageReceipts']}:
        assert time.monotonic() < deadline, 'the held mailbox was never re-read'
        time.sleep(0.01)
    _join_refreshes()
    _assert_same_as_today(_activity(), team.master)


def test_a_committed_write_whose_receipt_names_no_version_still_succeeds(team, monkeypatch):
    """A CAS put commits exactly expected_version + 1. The held revision follows from the
    CAS, never from parsing the receipt: an authority answering without ``version`` must
    not turn a committed message into a raised failure and a forgotten mailbox."""
    command = team.client.command

    def bare_receipt(operation, payload, command_id):
        command(operation, payload, command_id)
        return {'key': payload['key']}

    monkeypatch.setattr(team.client, 'command', bare_receipt)
    sent = _seed(team)
    team.scope_reads.clear()
    row = _activity()
    assert row['messageReceiptsAvailable'] is True
    assert sent['id'] in {receipt['messageId'] for receipt in row['messageReceipts']}
    _join_refreshes()
    _assert_same_as_today(_activity(), team.master)


def test_a_refresh_queued_behind_teardown_reads_nothing(team, monkeypatch):
    _seed(team)
    _activity()
    _join_refreshes()
    reads = []
    monkeypatch.setattr(message_receipts, '_project_receipts',
                        lambda *args, **kwargs: reads.append(1) or ({}, [], 0))
    monkeypatch.setattr(type(team.master), 'is_terminated', property(lambda self: True))
    message_receipts.refresh_resident_receipts(team.master)
    assert reads == []
