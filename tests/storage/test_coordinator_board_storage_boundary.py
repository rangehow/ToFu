"""Benign SQLite witnesses for the frozen coverage/storage extension boundary."""
from copy import deepcopy

import pytest

from lib.conversations.coordinator_progress import CoordinatorTaskProgress
from lib.project_dispatch.repository import CONTRACT, ProjectDispatchError
from lib.project_dispatch.segmented import SegmentedDispatch, key
from lib.storage.errors import StorageError
from lib.storage.generated_project_dispatch_contract import SPEC
from tests.project_mod.test_project_dispatch_repository import context
from tests.project_mod.test_coordinator_workers import runtime, base_runtime
from tests.storage.test_coordinator_segmented_allowance import segmented_runtime
__all__ = ['segmented_runtime']

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = [pytest.mark.api, pytest.mark.parametrize('semantic_authority', ['sqlite'], indirect=True)]


@pytest.fixture
def board(segmented_runtime):
    r = segmented_runtime
    scope = {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1}
    progress = CoordinatorTaskProgress(7, '/project', client=r.c.repo.client,
        bindings=r.c.binding, notify=lambda: None, arm=lambda *args: None)
    return r, progress, scope


def publish(progress, scope, count):
    progress.publish(scope, {'summary': 'Synthetic coverage boundary', 'tasks': [
        {'title': f'Outcome {number}', 'status': 'pending'} for number in range(count)]})
    return progress.read_for_coordinator(scope)['taskKeys']


def delegate(r, command, keys=None):
    result = r.call('delegate_project_work', **{**r.args, 'idempotency_key': command},
                    **({} if keys is None else {'board_task_keys': keys}))
    assert result.get('dispatch') == 'accepted', result
    return r.c.repo.read_point(result['assignment_id'])


def test_exact_evidence_retains_unknown_empty_many_links_and_retired_epoch(board):
    r, progress, scope = board
    keys = publish(progress, scope, 2)
    linked = delegate(r, 'linked', keys)
    second = delegate(r, 'second-link', keys[:1])
    unknown = delegate(r, 'unknown')
    empty = delegate(r, 'explicit-none', [])
    assert linked['request']['boardTaskKeys'] == keys
    assert second['request']['boardTaskKeys'] == keys[:1]
    assert 'boardTaskKeys' not in unknown['request']
    assert empty['request']['boardTaskKeys'] == []
    page_rows = CONTRACT['$defs']['historyPage']['properties']['assignments']['maxItems']
    for number in range(page_rows + 1):
        r.c.repo.authorize(r.c.request, r.c.principal, f'unrelated-{number}')
    freshest = r.c.repo.history_page()['rows']
    assert linked['id'] not in {row['id'] for row in freshest}
    assert r.c.repo.read_point(linked['id']) == linked

    real, queries = r.c.repo.client, []
    class Counted:
        def query(self, operation, payload, **kwargs):
            queries.append((operation, payload))
            return real().query(operation, payload, **kwargs)
        def query_many(self, requests, **kwargs):
            queries.extend(requests)
            return real().query_many(requests, **kwargs)
    r.c.repo.client = lambda **kwargs: Counted()
    assert r.c.repo.read_point(linked['id']) == linked
    assert not any(operation in ('project.dispatch.list', 'record.get') for operation, _ in queries)
    assert len(queries) == 2  # head plus exactly the caller-known assignment
    r.c.repo.client = real

    r.c.binding.value['revision'] = 2
    current_scope = {**scope, 'bindingRevision': 2}
    current_keys = publish(progress, current_scope, 1)
    stale = r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'stale-binding'},
                   board_task_keys=current_keys)
    assert 'Current coordination binding epoch' in stale['error']
    r.task.pop('_coordinatorDispatchScope', None)
    current = delegate(r, 'current-binding', current_keys)
    assert current['grant']['bindingRevision'] == 2
    assert r.c.repo.read_point(linked['id'])['grant']['bindingRevision'] == 1
    assert current['request']['coordinatorId'] == linked['request']['coordinatorId']
    for owner, project in ((8, '/project'), (7, '/another-project')):
        foreign = type(r.c.repo)(owner, project, client=real,
            conversations=r.c.conversations, binding_repository=r.c.binding,
            turn_get=r.c.repo.turn_get)
        with pytest.raises(ProjectDispatchError):
            foreign.read_point(linked['id'])

    # A fresh board replacement is not a replacement of assignment evidence.
    publish(progress, current_scope, 0)
    assert r.c.repo.read_point(current['id']) == current
    assert r.c.repo.read_point(linked['id']) == linked


def test_valid_many_link_admission_cannot_add_one_transaction_change_per_link(board, record_property):
    r, progress, scope = board
    count = SPEC['limits']['commitChanges'] + 1
    keys = publish(progress, scope, count)
    linked = delegate(r, 'many-links', keys)
    assert len(linked['request']['boardTaskKeys']) == count
    assert delegate(r, 'many-links', keys) == linked
    store = SegmentedDispatch(r.c.repo)
    head = store.head()
    document = store.query('get', collection='assignments', id=key(linked['id']))
    assert document['value']['record'] == linked
    # Hypothetical per-link repository indexes use the existing valid index
    # shape. Their atomic fanout alone exceeds the unchanged wire contract.
    additions = [{'collection': 'indexes', 'id': 'board:' + task_key + ':1',
                  'expected_version': 0,
                  'value': {'id': linked['id'], 'sequence': document['value']['sequence']}}
                 for task_key in keys]
    with pytest.raises(StorageError) as failure:
        store.command(head['version'], deepcopy(head['value']['state']), additions)
    assert failure.value.code == 'database_protocol_error'
    assert store.head() == head
    assert store.query('get', collection='indexes', id=additions[0]['id']) is None
    assert r.c.repo.read_point(linked['id']) == linked
    record_property('valid_link_count', count)
    record_property('atomic_change_limit', SPEC['limits']['commitChanges'])
