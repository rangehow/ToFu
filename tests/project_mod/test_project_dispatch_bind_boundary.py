"""Ordinary fresh-bind authority and immutable already-bound evidence; no executor."""
from copy import deepcopy

import pytest

from lib.project_dispatch.repository import ProjectDispatchError
from tests.project_mod.test_project_dispatch_repository import authorize, context as context

pytestmark = pytest.mark.unit


def test_fresh_bind_uses_exact_current_admission(context):
    c = context
    row = authorize(c)
    admission = c.repo.admission(row['id'])
    bound = c.repo.bind_execution(row['id'], 'worker-turn', 'worker-attempt',
                                 task_id='worker-task', admission=admission)
    assert bound['execution'] == {'turnId': 'worker-turn', 'attemptId': 'worker-attempt',
                                  'taskId': 'worker-task'}
    assert bound['request'] == row['request'] and bound['grant'] == row['grant']
    assert c.repo.read(row['id']) == bound


@pytest.mark.parametrize('field,value', [
    ('owner', 8), ('tenant', 'other'), ('projectPath', '/other'),
    ('recipientId', 'other'), ('revision', 2), ('assignmentId', 'other'),
])
def test_fresh_bind_denies_nonmatching_admission(context, field, value):
    c = context
    row = authorize(c)
    admission = {**c.repo.admission(row['id']), field: value}
    puts = c.client.puts
    with pytest.raises(ProjectDispatchError):
        c.repo.bind_execution(row['id'], 'worker-turn', 'worker-attempt', admission=admission)
    assert c.repo.read(row['id']) == row
    assert c.client.puts == puts


@pytest.mark.parametrize('change', ['binding_epoch', 'source_revision', 'revoked'])
def test_registered_unstarted_bind_denies_withdrawn_authority(context, change):
    c = context
    row = authorize(c)
    admission = c.repo.admission(row['id'])
    if change == 'binding_epoch':
        c.binding.value['revision'] += 1
    elif change == 'source_revision':
        c.turn['projectionRevision'] += 1
    else:
        c.repo.revoke(row['id'], 1)
    before = c.repo.read(row['id'])
    puts = c.client.puts
    with pytest.raises(ProjectDispatchError):
        c.repo.bind_execution(row['id'], 'worker-turn', 'worker-attempt', admission=admission)
    assert c.repo.read(row['id']) == before
    assert before['execution'] is None and c.client.puts == puts


def test_already_bound_replay_is_evidence_not_fresh_authority(context):
    c = context
    row = authorize(c)
    admission = c.repo.admission(row['id'])
    bound = c.repo.bind_execution(row['id'], 'worker-turn', 'worker-attempt', admission=admission)
    c.repo.revoke(row['id'], 1)
    c.binding.value['revision'] += 1
    c.turn['projectionRevision'] += 1
    retained = deepcopy(c.repo.read(row['id']))
    puts = c.client.puts
    replay = c.repo.bind_execution(row['id'], 'worker-turn', 'worker-attempt', admission=admission)
    assert replay == retained and replay['execution'] == bound['execution']
    assert c.client.puts == puts
    with pytest.raises(ProjectDispatchError):
        c.repo.validate(admission)
    with pytest.raises(ProjectDispatchError, match='immutable'):
        c.repo.bind_execution(row['id'], 'worker-turn', 'another-attempt', admission=admission)
    assert c.repo.read(row['id']) == retained
