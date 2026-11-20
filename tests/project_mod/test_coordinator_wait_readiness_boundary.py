"""Settlement waits cannot certify undispatched dependency readiness."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.project_mod.test_coordinator_continuation import (
    run, runtime, base_runtime, context, delegate, end,
)
from lib.project_dispatch import continuation
from lib.project_dispatch.delegation import review_worker
__all__ = ['run']

pytestmark = pytest.mark.unit


def test_same_ordinary_wait_evidence_spans_blocked_ready_and_unknown(run, monkeypatch):
    r = run
    parent = delegate(r)
    end(r, parent)
    checkpoint = 'a' * 40
    workspace = r.workspaces[parent['execution']['taskId']]
    workspace['checkpoint_sha'] = checkpoint
    workspace['origin']['delivery']['checkpointSha'] = checkpoint
    request = deepcopy(parent['request'])
    request['dependencies'] = [{'assignmentId': parent['id'], 'grantRevision': 1,
                                'resultRevision': 1, 'checkpointSha': checkpoint}]
    child = r.c.repo.authorize_from_coordinator(request, task=r.task, command_id='dependent')
    assert child['execution'] is None
    scope = {'user_id': 7, 'project_path': '/project', 'conversation_id': 'coordinator',
             'binding_revision': 1}

    def observe(expected):
        before = deepcopy(r.c.repo._load()[0])
        dependency_reads = []
        dependencies = r.c.repo.dependency_states
        with monkeypatch.context() as patch:
            def tracked(*args, **kwargs):
                dependency_reads.append(args[0]['id'])
                return dependencies(*args, **kwargs)
            patch.setattr(r.c.repo, 'dependency_states', tracked)
            witness = continuation.pre_park_witness(r.c.repo, before, 'coordinator-attempt')
        assert dependency_reads[0] == child['id']
        assert set(dependency_reads) <= {child['id'], parent['id']}
        assert witness[expected] == [child['id']]
        assert parent['id'] not in sum((witness[key] for key in ('ready', 'blocked', 'unknown')), [])
        assert continuation.pre_park_witness(r.c.repo, before, 'other-request')['ready'] == []
        assert r.c.repo._load()[0] == before
        return r.service.view_for_coordinator(child['id'], scope=scope, settlement_only=True)

    blocked = observe('blocked')
    assert r.c.repo.dependency_states(child)[0]['status'] == 'waiting_review'
    # Consume existing result news and the ordinary request-wide seeding read.
    r.call('poll', ids=[child['id']], wait_s=0)
    before_calls = list(r.calls)

    def ordinary_wait():
        with monkeypatch.context() as patch:
            patch.setattr(r.c.repo, 'dependency_states', lambda *a, **kw: pytest.fail('wait read readiness'))
            patch.setattr(r.c.repo, 'history_page', lambda *a, **kw: pytest.fail('wait scanned history'))
            patch.setattr(continuation, 'pre_park_witness', lambda *a: pytest.fail('wait computed witness'))
            return r.call('poll', ids=[child['id']], wait_s=0)

    waiting = ordinary_wait()
    assert waiting['workers'] == []
    assert waiting['pending'] == {'running': [child['id']], 'delivering': []}
    assert waiting['wait'] == 'timeout'
    review_worker(r.c.repo, r.task, {'assignment_id': parent['id'], 'decision': 'accept',
        'note': 'Verified', 'result_revision': 1, 'checkpoint_sha': checkpoint})
    assert r.c.repo.dependency_states(child)[0]['status'] == 'ready'
    ready = observe('ready')
    assert ready == blocked
    assert ready['execution'] is None and 'dependencyStates' not in ready
    ready_news = ordinary_wait()
    assert [row['assignment_id'] for row in ready_news['workers']] == [parent['id']]
    assert ready_news['wait'] == 'progress'
    assert ready_news['pending'] == waiting['pending']
    assert ordinary_wait() == waiting
    before = deepcopy(r.c.repo._load()[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(r.call, 'poll', ids=[child['id']], wait_s=0) for _ in range(2)]
        assert [future.result() for future in futures] == [waiting, waiting]
    assert r.c.repo._load()[0] == before
    ready_value = deepcopy(r.c.repo._load()[0])
    r.workspaces.pop(parent['execution']['taskId'])
    assert r.c.repo._load()[0] == ready_value
    assert r.c.repo.dependency_states(child)[0]['status'] == 'unknown'
    unknown = observe('unknown')
    assert unknown == ready
    unknown_wait = ordinary_wait()
    assert unknown_wait['workers'] == []
    assert unknown_wait['wait'] == 'timeout'
    assert unknown_wait['pending'] == {'running': [child['id']], 'delivering': [parent['id']]}
    before = deepcopy(r.c.repo._load()[0])
    assert ordinary_wait() == unknown_wait
    assert r.c.repo._load()[0] == before
    assert r.calls == before_calls and not r.scheduled and not r.reports
    assert r.c.repo.read(child['id'])['execution'] is None


@pytest.mark.parametrize('status', ['running', 'held'])
def test_dispatched_or_held_is_not_an_undispatched_ready_receipt(run, status):
    r = run
    worker = delegate(r)
    if status == 'held':
        from lib.turn_verdict import LEAD_HOLD
        r.attempts[worker['execution']['attemptId']]['status'] = 'interrupted'
        r.turns[worker['execution']['turnId']]['settlement'] = {'cause': LEAD_HOLD}
    before = deepcopy(r.c.repo._load()[0])
    witness = continuation.pre_park_witness(r.c.repo, before, 'coordinator-attempt')
    assert witness['ready'] == witness['blocked'] == witness['unknown'] == []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(r.call, 'poll', ids=[worker['id']], wait_s=0) for _ in range(2)]
        results = [future.result() for future in futures]
    for result in results:
        assert result['workers'] == []
        if status == 'held':
            assert result['held'] == [worker['id']]
            assert result['pending']['running'] == []
        else:
            assert result['pending']['running'] == [worker['id']]
    assert r.c.repo._load()[0] == before
    assert len(r.calls) == len(r.created) == 1
    assert not r.scheduled and not r.reports
