"""Executable, non-registered SQLite candidate; no native build or live state."""
from copy import deepcopy
import time
import uuid

import pytest

from lib.conversations.coordinator_progress import CoordinatorTaskProgress
from lib.project_dispatch.segmented import SegmentedDispatch, key
from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
from lib.storage_authority_sql.operations import resolve_operation
from lib.storage_authority_sql.operations_pkg import _project_dispatch as dispatch
from tests.project_mod.test_project_dispatch_repository import context
from tests.project_mod.test_coordinator_workers import runtime, base_runtime
from tests.storage_authority_sql.test_sqlite_commit_delivery import CommitFault
from tests.storage_authority_sql.test_sqlite_read_pool import _config
from tests.support import board_membership_candidate as candidate
__all__ = ['runtime']

pytestmark = pytest.mark.unit


class Client:
    def __init__(self, backend):
        self.backend, self.derive, self.reads = backend, True, []
        self.applies, self.commands, self.applied_commands = 0, [], []
        self.before_membership = None

    def __call__(self, **kwargs):
        return self

    def query(self, operation, payload, **kwargs):
        self.reads.append((operation, deepcopy(payload)))
        _, handler = resolve_operation(operation, 'query', payload)
        return self.backend.query(operation, handler, time.monotonic() + 30)

    def command(self, operation, payload, command_id, **kwargs):
        receipt, handler = resolve_operation(operation, 'command', payload)
        if self.before_membership and operation == 'project.dispatch.commit' and any(
                change.get('collection') == 'assignments' and change.get('expected_version') == 0
                and 'boardTaskKeys' in change.get('value', {}).get('record', {}).get('request', {})
                for change in payload['changes']):
            self.before_membership()
            self.before_membership = None
        def apply(session):
            self.applies += 1
            self.applied_commands.append((operation, command_id))
            if operation == 'project.dispatch.commit' and self.derive:
                return candidate.commit(session, payload)
            return handler(session)
        result = self.backend.command(operation, candidate.digest(payload), command_id, 'user',
                                      apply, time.monotonic() + 30, receipt_required=receipt)
        self.commands.append((operation, deepcopy(payload), command_id, result))
        return result

    def membership(self, scope, task):
        payload = {'user_id': scope['owner'], 'project_path': scope['projectPath'],
                   'coordinator_id': scope['coordinatorId'], 'binding_revision': scope['bindingRevision'],
                   'task_key': task, 'format': candidate.FORMAT}
        return self.backend.query('fixture.board.membership', lambda session: candidate.query(session, payload),
                                  time.monotonic() + 30)


@pytest.fixture
def fixture(runtime, tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    backend = SQLiteBackend(_config(tmp_path))
    backend.start()
    c = runtime.c
    client = Client(backend)
    for (namespace, identity), row in c.client.rows.items():
        client.command('record.put', {'namespace': namespace, 'key': identity,
            'expected_version': 0, 'value': row['value']}, uuid.uuid4().hex)
    c.client = client
    c.repo.client = client
    c.repo.storage_version = 2
    c.runtime = runtime
    c.store = SegmentedDispatch(c.repo)
    c.scope = {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1}
    c.progress = CoordinatorTaskProgress(7, '/project', client=c.client,
        bindings=c.binding, notify=lambda: None, arm=lambda *args: None)
    try:
        yield c
    finally:
        backend.close()


def board(c, count, scope=None):
    scope = scope or c.scope
    c.progress.publish(scope, {'summary': 'Synthetic candidate', 'tasks': [
        {'title': f'Outcome {i}', 'status': 'pending'} for i in range(count)]})
    return c.progress.read_for_coordinator(scope)['taskKeys']


def authorize(c, command, keys=None):
    r = c.runtime
    result = r.call('delegate_project_work', **{**r.args, 'idempotency_key': command},
                    **({} if keys is None else {'board_task_keys': keys}))
    assert result.get('dispatch') == 'accepted', result
    return c.repo.read_point(result['assignment_id'])


def raw_update(c, record, **options):
    document = c.store.query('get', collection='assignments', id=key(record['id']))
    payload = {**c.store.scope, 'changes': [{'collection': 'assignments', 'id': key(record['id']),
        'expected_version': document['version'], 'value': {**document['value'], 'record': record}}]}
    return c.client.command('project.dispatch.commit', payload, options.get('command', uuid.uuid4().hex))


def test_513_committed_keys_same_receipt_replay_and_lifecycle(fixture, record_property):
    c = fixture
    keys = board(c, 513)
    row = authorize(c, 'many', keys)
    assert len(row['request']['boardTaskKeys']) == 513
    document = c.store.query('get', collection='assignments', id=key(row['id']))
    assert authorize(c, 'many', keys) == row
    assert c.store.query('get', collection='assignments', id=key(row['id'])) == document
    admission = next(item for item in c.client.commands if item[0] == 'project.dispatch.commit'
        and any(change.get('value', {}).get('record', {}).get('request', {}).get('boardTaskKeys') == keys
                for change in item[1]['changes']))
    assert len(admission[1]['changes']) < 513
    applies = c.client.applies
    assert c.client.command(*admission[:3]) == admission[3]
    assert c.client.applies == applies
    for task in keys:
        found = c.client.membership(c.scope, task)
        assert [item['assignment'] for item in found['matches']] == [row]
        assert found['coverage'] == 'unknown' and not found['complete']
    _, _, project = dispatch._scope(c.store.scope)
    prefix = f'{project}:indexes:{candidate.PREFIX}'
    measured = c.client.backend.query('fixture.index.size', lambda session: session.fetch_one(
        'SELECT COUNT(*) AS rows, SUM(length(value_json)) AS bytes FROM storage_records '
        'WHERE namespace=? AND record_key>=? AND record_key<?',
        (dispatch.DOCUMENTS, prefix, prefix + '\uffff')), time.monotonic() + 30)
    assert measured['rows'] == 2 * len(keys) + 1
    record_property('candidate_index_rows', measured['rows'])
    record_property('candidate_index_json_bytes', measured['bytes'])
    second = authorize(c, 'second', keys[:1])
    assert {m['assignment']['id'] for m in c.client.membership(c.scope, keys[0])['matches']} == {row['id'], second['id']}
    c.repo.revoke(row['id'], row['grant']['revision'])
    retained = c.repo.read_point(row['id'])
    assert retained['grant']['revoked']
    assert c.client.membership(c.scope, keys[-1])['matches'][0]['assignment'] == retained
    before = c.store.head()
    changed = deepcopy(retained)
    changed['request']['boardTaskKeys'] = []
    with pytest.raises(StorageError, match='Immutable'):
        raw_update(c, changed)
    assert c.repo.read_point(row['id']) == retained and c.store.head() == before
    with pytest.raises(StorageError):
        c.store.command(before['version'], before['value']['state'], [{'collection': 'assignments',
            'id': key(row['id']), 'expected_version': 2, 'delete': True}])
    board(c, 0)
    assert c.client.membership(c.scope, keys[-1])['matches'][0]['assignment'] == retained
    record_property('committed_link_count', len(keys))


def test_exact_binding_owner_project_and_unknown_coverage(fixture):
    c = fixture
    keys = board(c, 2)
    old = authorize(c, 'old', keys)
    authorize(c, 'omitted')
    authorize(c, 'empty', [])
    c.client.derive = False
    preindex = authorize(c, 'preindex', keys[:1])
    legacy = authorize(c, 'legacy', keys[:1])
    legacy_document = c.store.query('get', collection='assignments', id=key(legacy['id']))
    legacy['grant'].pop('bindingRevision')
    raw_update(c, legacy)
    c.client.derive = True
    raw_update(c, legacy)
    marker = c.store.query('get', collection='indexes', id=candidate.PREFIX + 'source:' + key(legacy['id']))
    assert marker['value']['linkage']['bindingRevision'] is None
    assert legacy_document['value']['record']['grant']['bindingRevision'] == 1
    c.binding.value['revision'] = 2
    current_scope = {**c.scope, 'bindingRevision': 2}
    current_keys = board(c, 1, current_scope)
    c.runtime.task.pop('_coordinatorDispatchScope', None)
    current = authorize(c, 'current', current_keys)
    assert current['grant']['bindingRevision'] == 2
    assert [m['assignment']['id'] for m in c.client.membership(c.scope, keys[0])['matches']] == [old['id']]
    assert current_keys[0] == keys[0]
    assert [m['assignment']['id'] for m in c.client.membership(current_scope, keys[0])['matches']] == [current['id']]
    assert [m['assignment']['id'] for m in c.client.membership(current_scope, current_keys[0])['matches']] == [current['id']]
    for scope in ({**c.scope, 'owner': 8}, {**c.scope, 'projectPath': '/other'},
                  {**c.scope, 'coordinatorId': 'different'}, {**c.scope, 'bindingRevision': 3}):
        answer = c.client.membership(scope, keys[0])
        assert answer['matches'] == [] and answer['coverage'] == 'unknown' and not answer['complete']
    assert preindex['id'] not in {m['assignment']['id'] for m in c.client.membership(c.scope, keys[0])['matches']}
    assert c.client.membership(c.scope, 'f' * 32)['coverage'] == 'unknown'


def test_posting_failure_rolls_back_source_head_counts_and_receipt(fixture, monkeypatch):
    c = fixture
    keys = board(c, 2)
    # Establish the ordinary segmented head before injecting a derived failure.
    authorize(c, 'baseline', [])
    head = c.store.head()
    original, writes = candidate.put_index, []
    def fail_late(session, payload, identity, value):
        result = original(session, payload, identity, value)
        writes.append(identity)
        if len(writes) == 4:
            raise StorageError('database_integrity', 'injected posting failure')
        return result
    monkeypatch.setattr(candidate, 'put_index', fail_late)
    result = c.runtime.call('delegate_project_work', **{**c.runtime.args, 'idempotency_key': 'rollback'},
                            board_task_keys=keys)
    assert result['code'] == 'database_integrity' and result['error'] == 'injected posting failure'
    assert c.store.head() == head
    assert len(writes) == 4
    for identity in writes:
        assert c.store.query('get', collection='indexes', id=identity) is None
    source_id = writes[0].removeprefix(candidate.PREFIX + 'source:')
    assert c.store.query('get', collection='assignments', id=source_id) is None
    assert c.client.membership(c.scope, keys[0])['matches'] == []
    assert c.client.membership(c.scope, keys[1])['matches'] == []
    monkeypatch.setattr(candidate, 'put_index', original)
    row = authorize(c, 'rollback', keys)
    assert c.store.query('get', collection='assignments', id=key(row['id']))['version'] == 2
    assert c.client.membership(c.scope, keys[0])['matches'][0]['assignment'] == row


def test_real_commit_ack_loss_recovers_one_receipt_without_rederivation(fixture):
    c = fixture
    keys = board(c, 2)
    row = authorize(c, 'seed', keys)
    updated = deepcopy(row)
    updated['modelAttempts'] = []
    backend = c.client.backend
    fault = CommitFault(backend._writer._connection, phase='after')
    backend._writer._connection = fault
    before = c.client.applies
    receipt = raw_update(c, updated, command='lost-ack')
    assert fault.commits == 2 and c.client.applies == before + 1
    assert receipt['version'] == 3
    assert c.client.membership(c.scope, keys[0])['matches'][0]['sourceVersion'] == 3
    assert len(c.client.membership(c.scope, keys[1])['matches']) == 1
    operation, payload, command, original = c.client.commands[-1]
    assert c.client.command(operation, payload, command) == original == receipt
    assert c.client.applies == before + 1


def test_old_writer_removal_detected_and_history_not_selected(fixture, monkeypatch):
    c = fixture
    keys = board(c, 1)
    row = authorize(c, 'linked', keys)
    reads = []
    original, original_list = dispatch.get, dispatch.list_documents
    pages = []
    def counted_page(session, payload):
        pages.append((payload['collection'], payload['prefix']))
        return original_list(session, payload)
    monkeypatch.setattr(dispatch, 'list_documents', counted_page)
    def counted(session, payload):
        reads.append((payload.get('collection'), payload.get('id')))
        return original(session, payload)
    monkeypatch.setattr(dispatch, 'get', counted)
    first = c.client.membership(c.scope, keys[0])
    cost = len(reads)
    for number in range(80):
        authorize(c, f'unrelated-{number}', [])
    reads.clear()
    pages.clear()
    assert c.client.membership(c.scope, keys[0]) == first
    assert len(reads) == cost == 2  # one count and one matching assignment
    assert pages and all(collection == 'indexes' and prefix.startswith(candidate.PREFIX)
                         for collection, prefix in pages)
    assert c.repo.read_point(row['id']) == row
    prefix = candidate.scope_prefix('coordinator', 1, keys[0])
    identity = prefix + 'p:' + key(row['id'])
    posting = c.store.query('get', collection='indexes', id=identity)
    head = c.store.head()
    deletion = [{'collection': 'indexes', 'id': identity, 'expected_version': posting['version'], 'delete': True}]
    with pytest.raises(StorageError, match='authority-owned'):
        c.store.command(head['version'], head['value']['state'], deletion)
    c.client.derive = False
    c.store.command(head['version'], head['value']['state'], deletion)
    c.client.derive = True
    with pytest.raises(StorageError, match='count differs'):
        c.client.membership(c.scope, keys[0])
    assert c.repo.read_point(row['id']) == row


def test_positive_pages_and_old_writer_source_mutation_fail_closed(fixture):
    c = fixture
    task = board(c, 1)[0]
    count = dispatch.LIMITS['pageRows'] + 1
    rows = [authorize(c, f'linked-{number}', [task]) for number in range(count)]
    answer = c.client.membership(c.scope, task)
    assert {m['assignment']['id'] for m in answer['matches']} == {row['id'] for row in rows}
    assert len(answer['matches']) == count and not answer['complete']
    changed = deepcopy(rows[0])
    changed['request']['boardTaskKeys'] = []
    c.client.derive = False
    raw_update(c, changed)
    c.client.derive = True
    with pytest.raises(StorageError, match='immutable source'):
        c.client.membership(c.scope, task)


def test_candidate_is_not_a_registered_product_operation(fixture):
    for kind in ('query', 'command'):
        with pytest.raises(StorageError, match='Unknown storage operation'):
            resolve_operation('project.dispatch.board-membership-v1', kind, {})


def test_first_membership_derivation_survives_real_commit_ack_loss(fixture):
    c = fixture
    keys = board(c, 2)
    authorize(c, 'initialize')
    faults = []
    def install():
        backend = c.client.backend
        fault = CommitFault(backend._writer._connection, phase='after')
        backend._writer._connection = fault
        faults.append(fault)
    c.client.before_membership = install
    row = authorize(c, 'first-link-ack-loss', keys)
    admission = next(item for item in c.client.commands if item[0] == 'project.dispatch.commit'
        and any(change.get('expected_version') == 0
                and change.get('value', {}).get('record', {}).get('id') == row['id']
                for change in item[1]['changes']))
    assert len(faults) == 1 and faults[0].commits >= 2
    assert c.client.applied_commands.count((admission[0], admission[2])) == 1
    assert c.client.command(*admission[:3]) == admission[3]
    assert c.client.applied_commands.count((admission[0], admission[2])) == 1
    marker = c.store.query('get', collection='indexes', id=candidate.PREFIX + 'source:' + key(row['id']))
    assert marker['version'] == 1
    for task in keys:
        prefix = candidate.scope_prefix('coordinator', 1, task)
        posting = c.store.query('get', collection='indexes', id=prefix + 'p:' + key(row['id']))
        count = c.store.query('get', collection='indexes', id=prefix + 'count')
        assert posting['version'] == count['version'] == 1 and count['value']['count'] == 1
        assert [m['assignment'] for m in c.client.membership(c.scope, task)['matches']] == [row]


@pytest.mark.parametrize('bad_count', [
    {'format': 'unsupported', 'count': 1},
    {'format': candidate.FORMAT, 'count': True},
])
def test_unsupported_count_cannot_be_normalized_by_new_admission(fixture, bad_count):
    c = fixture
    task = board(c, 1)[0]
    row = authorize(c, 'linked', [task])
    identity = candidate.scope_prefix('coordinator', 1, task) + 'count'
    count = c.store.query('get', collection='indexes', id=identity)
    head = c.store.head()
    c.client.derive = False
    c.store.command(head['version'], head['value']['state'], [{'collection': 'indexes', 'id': identity,
        'expected_version': count['version'], 'value': bad_count}])
    c.client.derive = True
    before = c.store.head()
    result = c.runtime.call('delegate_project_work', **{**c.runtime.args, 'idempotency_key': 'rejected'},
                            board_task_keys=[task])
    assert result['code'] == 'database_integrity'
    assert c.store.head() == before
    assert c.store.query('get', collection='indexes', id=identity)['value'] == bad_count
    assert c.repo.read_point(row['id']) == row
    with pytest.raises(StorageError):
        c.client.membership(c.scope, task)


@pytest.mark.parametrize('guard', ['source', 'marker'])
def test_linkage_representation_change_is_atomic_and_replay_remains_valid(fixture, guard):
    c = fixture
    task = board(c, 1)[0]
    row = authorize(c, 'representation', [task])
    source_id = key(row['id'])
    source = c.store.query('get', collection='assignments', id=source_id)
    marker_id = candidate.PREFIX + 'source:' + source_id
    marker = c.store.query('get', collection='indexes', id=marker_id)
    head = c.store.head()
    if guard == 'marker':
        changed_marker = deepcopy(marker['value'])
        changed_marker['linkage']['sequence'] = float(changed_marker['linkage']['sequence'])
        assert changed_marker == marker['value']
        c.client.derive = False
        c.store.command(head['version'], head['value']['state'], [{'collection': 'indexes',
            'id': marker_id, 'expected_version': marker['version'], 'value': changed_marker}])
        c.client.derive = True
    baseline_head = c.store.head()
    baseline_marker = c.store.query('get', collection='indexes', id=marker_id)
    changed_source = deepcopy(source['value'])
    if guard == 'source':
        changed_source['sequence'] = float(changed_source['sequence'])
        assert changed_source == source['value']
    payload = {**c.store.scope, 'changes': [{'collection': 'assignments', 'id': source_id,
        'expected_version': source['version'], 'value': changed_source}]}
    with pytest.raises(StorageError, match='linkage changed|marker changed'):
        c.client.command('project.dispatch.commit', payload, 'representation-rejected')
    assert c.store.query('get', collection='assignments', id=source_id) == source
    assert c.store.query('get', collection='indexes', id=marker_id) == baseline_marker
    assert c.store.head() == baseline_head
    assert [m['assignment'] for m in c.client.membership(c.scope, task)['matches']] == [row]
    if guard == 'marker':
        c.client.derive = False
        c.store.command(baseline_head['version'], baseline_head['value']['state'], [{'collection': 'indexes',
            'id': marker_id, 'expected_version': baseline_marker['version'], 'value': marker['value']}])
        c.client.derive = True
    payload['changes'][0]['value'] = deepcopy(source['value'])
    payload['changes'][0]['value']['record']['modelAttempts'] = []
    receipt = c.client.command('project.dispatch.commit', payload, 'representation-rejected')
    applies = c.client.applies
    assert c.client.command('project.dispatch.commit', payload, 'representation-rejected') == receipt
    assert c.client.applies == applies
    assert c.client.membership(c.scope, task)['matches'][0]['sourceVersion'] == receipt['version']
