"""Durable swarm lifecycle receipts on scratch SQLite and socket-only PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from threading import Event
import time
import uuid

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage_authority_sql.operations import resolve_operation_contract
from lib.swarm.message_contract import NAMESPACE
from lib.swarm.persistence_contract import (
    SWARM_DELETE_EXACT_OPERATION as DELETE,
    SWARM_RUN_RESULT_FORMAT,
    SWARM_RUN_RESULT_NAMESPACE,
    swarm_mailbox_key,
    swarm_run_result_key,
)
from tests.storage_authority_sql.test_native_checkpoint_handoff import TransactionClient
from tests.storage_authority_sql.test_sqlite_read_pool import _config

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.unit

LIFECYCLE = 'swarm.session.lifecycle.get'
OLD = dict(swarm_key='shared', run_id='old', owner_id=7, tenant_id='tenant',
           conversation_id='conversation', project_path='/canonical')
PARENT = dict(turn_id='turn', task_id='parent-task', attempt_id='attempt')
ROSTER = [{'id': 'child', 'role': 'coder', 'objective': 'keep evidence'}]


@pytest.fixture(params=['sqlite', 'postgres'])
def client(request, tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = _config(tmp_path)
    if request.param == 'postgres':
        from lib.storage_authority_sql.adapters.postgres import PostgresBackend

        dsn = request.getfixturevalue('postgres_cluster')
        assert "host=/" in dsn  # No TCP or inherited DSN.
        backend = PostgresBackend(replace(config, backend='postgres', postgres_dsn=dsn))
    else:
        from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend

        backend = SQLiteBackend(config)
    backend.start()
    try:
        yield TransactionClient(backend)
    finally:
        backend.close()


def command(client, operation, *, command_id=None, **payload):
    return client.command(operation, payload, command_id or uuid.uuid4().hex)


def save(client, identity=OLD, *, mode='launch', specs=ROSTER, **extra):
    if mode is not None:
        extra['save_mode'] = mode
    return command(client, 'swarm.session.save', now_ms=1,
                   swarm_key=identity['swarm_key'], task_id=identity['run_id'],
                   conv_id=identity['conversation_id'], specs=specs, config={
                       'user_id': identity['owner_id'], 'parent_cfg': {
                           '_tenant_id': identity['tenant_id'],
                           'canonicalProjectPath': identity['project_path'] + '/',
                           '_swarmParentTurnId': PARENT['turn_id'],
                           '_swarmParentTaskId': PARENT['task_id'],
                           '_swarmParentAttemptId': PARENT['attempt_id'],
                       }}, **extra)


def exact(client, disposition, identity=OLD, command_id=None):
    return command(client, DELETE, command_id=command_id, **identity, disposition=disposition)


def receipt(client, identity=OLD):
    return client.query(LIFECYCLE, identity)


def mailbox():
    return dict(namespace=NAMESPACE, key=swarm_mailbox_key(7, 'tenant', 'conversation', 'old', 'child'),
                value={'binding': dict(owner=7, tenant='tenant', conversationId='conversation',
                                       projectPath='/canonical', swarmKey='shared', swarmRunId='old',
                                       agentId='child'), 'closed': False, 'messages': []})


def record(client, address):
    return client.query('record.get', {key: address[key] for key in ('namespace', 'key')})


@pytest.mark.parametrize('winner', ['stop', 'replacement'])
@pytest.mark.parametrize('retire_first', [False, True])
def test_receipt_orders_dispositions_without_any_mailbox(client, winner, retire_first):
    first, second = ('ordinary_retirement', winner) if retire_first else (winner, 'ordinary_retirement')
    assert receipt(client) is None
    assert save(client)['saved']
    live = {'identity': OLD, 'parent': PARENT, 'roster': ROSTER, 'disposition': None}
    assert receipt(client) == live
    assert exact(client, first, command_id='first') == {'deleted': True, 'mailboxes_changed': 0}
    assert receipt(client) == {**live, 'disposition': first}

    successor = {**OLD, 'run_id': 'successor'}
    assert save(client, successor, specs=[])['saved']
    successor_receipt = {'identity': successor, 'parent': PARENT, 'roster': [], 'disposition': None}
    assert receipt(client, successor) == successor_receipt
    assert exact(client, second) == {'deleted': False, 'mailboxes_changed': 0}
    assert receipt(client) == {**live, 'disposition': winner}
    assert exact(client, first, command_id='first') == {'deleted': True, 'mailboxes_changed': 0}
    assert receipt(client) == {**live, 'disposition': winner}
    assert receipt(client, successor) == successor_receipt
    assert record(client, mailbox()) is None


@pytest.mark.parametrize('field,value', [('owner_id', 8), ('tenant_id', 'foreign'),
    ('conversation_id', 'foreign'), ('project_path', '/other'), ('swarm_key', 'other'),
    ('run_id', 'unknown')])
def test_receipt_cannot_be_read_or_disposed_under_other_identity(client, field, value):
    assert save(client)['saved']
    wrong = {**OLD, field: value}
    assert receipt(client, wrong) is None
    assert exact(client, 'stop', wrong) == {'deleted': False, 'mailboxes_changed': 0}
    assert receipt(client)['disposition'] is None
    assert exact(client, 'ordinary_retirement')['deleted']
    assert receipt(client, wrong) is None
    assert exact(client, 'stop', wrong) == {'deleted': False, 'mailboxes_changed': 0}
    assert receipt(client)['disposition'] == 'ordinary_retirement'


def archive_address():
    return dict(namespace=SWARM_RUN_RESULT_NAMESPACE, key=swarm_run_result_key(7, 'old', 'child'),
                value=dict(format=SWARM_RUN_RESULT_FORMAT, ownerId=7, tenantId='tenant',
                           conversationId='conversation', swarmKey='shared', swarmRunId='old',
                           agentId='child', role='coder', objective='keep evidence',
                           status='completed', result={'final_answer': 'kept'}))


def archived_checkpoint(client):
    archive = archive_address()
    command(client, 'swarm.agent.save', swarm_key='shared', run_id='old', agent_id='child',
            now_ms=2, role='coder', objective='keep evidence', messages=[],
            result=archive['value']['result'], status='completed')
    command(client, 'record.put', **archive, expected_version=0)
    return archive


def test_receipt_archive_and_mailbox_cas_share_rollback_boundary(client, monkeypatch):
    from lib.swarm import persistence

    monkeypatch.setattr(persistence, '_storage', lambda: client)
    assert save(client)['saved']
    before = receipt(client)
    archive = archived_checkpoint(client)
    address = mailbox()
    assert command(client, 'record.put', **address, expected_version=0)['version'] == 1
    payload = {**OLD, 'disposition': 'stop'}
    required, apply, timeout = resolve_operation_contract(DELETE, 'command', payload)
    assert required

    def fail_after_mutation(session):
        assert apply(session) == {'deleted': True, 'mailboxes_changed': 1}
        raise StorageError('database_internal', 'injected rollback after exact teardown')

    with pytest.raises(StorageError, match='injected rollback'):
        client.backend.command(DELETE, hashlib.sha256(orjson.dumps(payload)).hexdigest(),
            'retry', 'user', fail_after_mutation, time.monotonic() + 20,
            receipt_required=required, transaction_timeout_s=timeout)
    assert receipt(client) == before
    assert record(client, archive)['value'] == archive['value']
    assert record(client, archive)['version'] == 1
    assert record(client, address)['value'] == address['value']
    assert command(client, 'record.put', **address, expected_version=1)['version'] == 2
    assert exact(client, 'stop', command_id='retry') == {'deleted': True, 'mailboxes_changed': 1}
    assert receipt(client) == {**before, 'disposition': 'stop'}
    sealed = {**archive['value'], 'sealedByTeardown': True}
    assert record(client, archive)['value'] == sealed
    assert persistence.load_run_result(owner_id=7, swarm_run_id='old', agent_id='child') == sealed
    with pytest.raises(StorageError, match='disposed'):
        command(client, 'record.put', **archive, expected_version=2)
    assert record(client, archive)['version'] == 2
    assert record(client, address)['version'] == 3
    assert record(client, address)['value'] == {**address['value'], 'runDisposition': 'stop'}
    with pytest.raises(StorageError) as denied:
        command(client, 'record.put', **address, expected_version=2)
    assert denied.value.code in {'database_not_found', 'database_conflict'}
    assert record(client, address)['version'] == 3


def test_exact_teardown_adopts_legacy_run_without_mailbox(client):
    assert save(client, mode=None)['saved']
    assert receipt(client) is None
    assert exact(client, 'ordinary_retirement') == {'deleted': True, 'mailboxes_changed': 0}
    assert receipt(client) == {'identity': OLD, 'parent': PARENT, 'roster': ROSTER,
                               'disposition': 'ordinary_retirement'}
    with pytest.raises(StorageError) as denied:
        save(client)
    assert denied.value.code == 'database_conflict'
    assert exact(client, 'stop') == {'deleted': False, 'mailboxes_changed': 0}
    assert receipt(client)['disposition'] == 'stop'


@pytest.mark.parametrize('change', [
    {'conv_id': 'foreign'},
    {'config': {'user_id': 8, 'project_path': '/canonical'}},
    {'config': {'user_id': 7, 'project_path': '/canonical',
                'parent_cfg': {'_swarmParentTaskId': 'forged'}}},
    {'specs': [{'id': 'forged'}]},
])
def test_parentless_legacy_header_cannot_be_rebound_before_adoption(client, change):
    original = dict(swarm_key='shared', task_id='old', conv_id='conversation',
                    config={'user_id': 7, 'project_path': '/canonical'}, specs=ROSTER)
    assert command(client, 'swarm.session.save', now_ms=1, **original)['saved']
    before = client.query('swarm.session.get', {'swarm_key': 'shared'})
    with pytest.raises(StorageError) as denied:
        command(client, 'swarm.session.save', now_ms=2, **{**original, **change})
    assert denied.value.code == 'database_conflict'
    assert client.query('swarm.session.get', {'swarm_key': 'shared'}) == before
    assert receipt(client) is None
    # An unchanged legacy snapshot can still be saved and adopted.
    assert command(client, 'swarm.session.save', now_ms=3, **original)['saved']
    identity = {**OLD, 'tenant_id': ''}
    assert exact(client, 'stop', {**identity, 'owner_id': 8}) == {
        'deleted': False, 'mailboxes_changed': 0}
    assert exact(client, 'stop', identity) == {'deleted': True, 'mailboxes_changed': 0}
    assert receipt(client, identity) == {'identity': identity, 'parent': dict.fromkeys(PARENT, ''),
                                         'roster': ROSTER, 'disposition': 'stop'}


def test_stop_with_absent_archive_refuses_late_put(client):
    assert save(client)['saved']
    archive = archive_address()
    assert record(client, archive) is None
    assert exact(client, 'stop')['deleted']  # Roster member never checkpointed.
    before = receipt(client)
    with pytest.raises(StorageError) as denied:
        command(client, 'record.put', **archive, expected_version=0)
    assert denied.value.code == 'database_conflict'
    forged = {**archive, 'value': {**archive['value'], 'swarmRunId': 'another-run'}}
    with pytest.raises(StorageError, match='address'):
        command(client, 'record.put', **forged, expected_version=0)
    assert record(client, archive) is None
    assert receipt(client) == before


@pytest.mark.parametrize('rollback', [False, True], ids=['commit', 'rollback'])
def test_first_stop_orders_an_overlapping_absent_archive_put(client, rollback):
    assert save(client)['saved']
    archive = archive_address()
    payload = {**OLD, 'disposition': 'stop'}
    required, stop, timeout = resolve_operation_contract(DELETE, 'command', payload)
    put_payload = {**archive, 'expected_version': 0}
    put_required, put, put_timeout = resolve_operation_contract('record.put', 'command', put_payload)
    stopped, release, submitted, entered = Event(), Event(), Event(), Event()
    postgres_pid = None

    def hold_stop(session):
        outcome = stop(session)
        assert outcome == {'deleted': True, 'mailboxes_changed': 0}
        stopped.set()
        assert release.wait(15)
        if rollback:
            raise StorageError('database_internal', 'inject Stop rollback before archive put')
        return outcome

    def apply_put(session):
        nonlocal postgres_pid
        if session.backend == 'postgres':
            postgres_pid = session.fetch_one('SELECT pg_backend_pid() AS pid')['pid']
        entered.set()
        return put(session)

    def put_archive():
        submitted.set()
        return client.backend.command('record.put', hashlib.sha256(orjson.dumps(put_payload)).hexdigest(),
            'pending-archive', 'user', apply_put, time.monotonic() + 20,
            receipt_required=put_required, transaction_timeout_s=put_timeout)

    with ThreadPoolExecutor(max_workers=2) as pool:
        stop_future = pool.submit(client.backend.command, DELETE,
            hashlib.sha256(orjson.dumps(payload)).hexdigest(), 'first-stop', 'user', hold_stop,
            time.monotonic() + 20, receipt_required=required, transaction_timeout_s=timeout)
        try:
            assert stopped.wait(10)
            put_future = pool.submit(put_archive)
            assert submitted.wait(10)
            # SQLite queues behind its sole writer; PostgreSQL can enter the
            # concurrent transaction while Stop still owns the run lock.
            if client.backend.name == 'postgres':
                assert entered.wait(10)
                deadline = time.monotonic() + 5
                while not client.backend.query('test.archive_lock_wait', lambda session: session.fetch_one(
                    "SELECT 1 FROM pg_locks WHERE pid = ? AND locktype = 'advisory' AND NOT granted",
                    (postgres_pid,)), deadline):
                    assert not put_future.done(), 'archive put bypassed the in-flight Stop'
                    assert time.monotonic() < deadline, 'archive put did not wait on Stop'
                    time.sleep(0.01)
        finally:
            release.set()
        if rollback:
            with pytest.raises(StorageError, match='inject Stop rollback'):
                stop_future.result()
            assert put_future.result()['version'] == 1
        else:
            assert stop_future.result()['deleted']
            with pytest.raises(StorageError, match='disposed'):
                put_future.result()
    if rollback:
        assert receipt(client)['disposition'] is None
        assert record(client, archive)['value'] == archive['value']
    else:
        assert receipt(client)['disposition'] == 'stop'
        assert record(client, archive) is None


def test_disposed_run_result_reader_refuses_preexisting_unsealed_archive(client, monkeypatch):
    from lib.swarm import persistence

    monkeypatch.setattr(persistence, '_storage', lambda: client)
    assert save(client)['saved']
    archive = archive_address()
    command(client, 'record.put', **archive, expected_version=0)
    address = dict(owner_id=7, swarm_run_id='old', agent_id='child')
    assert persistence.load_run_result(**address) == archive['value']
    assert exact(client, 'stop')['deleted']  # No checkpoint to verify and seal the archive.
    with pytest.raises(StorageError) as denied:
        persistence.load_run_result(**address)
    assert denied.value.code == 'database_integrity'


def test_stale_roster_writer_cannot_change_old_receipt_or_successor(client):
    assert save(client)['saved']
    added = [{'id': 'second-child'}]
    assert save(client, mode='roster', specs=added)['saved']
    assert receipt(client)['roster'] == ROSTER + added
    assert exact(client, 'replacement')['deleted']
    retired = receipt(client)
    assert retired['roster'] == ROSTER + added
    successor = {**OLD, 'run_id': 'successor'}
    assert save(client, successor, specs=[])['saved']
    before = receipt(client, successor)
    with pytest.raises(StorageError) as denied:
        save(client, mode='roster', specs=[{'id': 'late-child'}])
    assert denied.value.code in {'database_not_found', 'database_conflict'}
    assert receipt(client) == retired
    assert receipt(client, successor) == before


def test_managed_key_and_generic_update_cannot_bypass_lifecycle(client):
    assert save(client)['saved']
    before = receipt(client)
    with pytest.raises(StorageError, match='accepted roster'):
        save(client, mode='update', specs=[], expected_updated_at=1)
    with pytest.raises(StorageError, match='accepted parent'):
        command(client, 'swarm.session.save', save_mode='update', now_ms=2,
                expected_updated_at=1, swarm_key='shared', task_id='old',
                conv_id='conversation', specs=ROSTER, config={'user_id': 7, 'parent_cfg': {
                    '_tenant_id': 'tenant', 'projectPath': '/canonical',
                    '_swarmParentTaskId': 'forged'}})
    with pytest.raises(StorageError, match='exact save mode'):
        save(client, mode=None)
    assert receipt(client) == before
    assert exact(client, 'ordinary_retirement')['deleted']
    with pytest.raises(StorageError, match='exact-run teardown'):
        command(client, 'swarm.session.delete', swarm_key='shared')
    with pytest.raises(StorageError, match='already consumed'):
        save(client, {**OLD, 'swarm_key': 'different'})
    successor = {**OLD, 'run_id': 'successor'}
    assert save(client, successor)['saved']
    with pytest.raises(StorageError, match='exact-run teardown'):
        command(client, 'swarm.session.delete', swarm_key='shared')
    for run_payload in ({}, {'run_id': 'old'}):
        with pytest.raises(StorageError):
            command(client, 'swarm.agent.save', swarm_key='shared', agent_id='child',
                    now_ms=2, messages=[], result={}, **run_payload)
    assert client.query('swarm.session.get', {'swarm_key': 'shared'})['task_id'] == 'successor'
    assert receipt(client, successor)['roster'] == ROSTER


def test_native_exact_storage_requests_fail_before_transport():
    from lib.storage.v2_semantics import request_payload

    for operation, payload in ((DELETE, OLD), (LIFECYCLE, OLD),
                               ('swarm.session.save', {'save_mode': 'launch'}),
                               ('swarm.agent.save', {'run_id': 'old'})):
        with pytest.raises(StorageError, match='Native exact swarm lifecycle is unsupported'):
            request_payload(operation, payload, owner_id=7)
