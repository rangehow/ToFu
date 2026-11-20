from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.llm.request_checkpoint import IdleRequestCheckpoints, request_checkpoint_key

pytestmark = pytest.mark.unit


def test_checkout_transfers_exclusive_ownership_and_eviction_disposes():
    disposed = []
    checkpoints = IdleRequestCheckpoints(1, lambda item, reason: disposed.append(item))
    checkpoints.park('old', 'old connection')
    checkpoints.park('current', 'previous connection')
    checkpoints.park('current', 'new connection')
    with ThreadPoolExecutor(max_workers=2) as workers:
        takes = list(workers.map(checkpoints.take, ['current', 'current']))
    assert takes.count('new connection') == 1
    assert takes.count(None) == 1
    assert disposed == ['old connection', 'previous connection']


def test_checkpoint_identity_is_scoped_to_owner_and_credential():
    def key(credential, owner, trace):
        return request_checkpoint_key(
            'https://example.invalid', {'Authorization': credential, 'M-TraceId': trace},
            'conversation', owner, transient_headers=frozenset({'m-traceid'}))

    assert key('one', 'owner', 'trace-a') == key('one', 'owner', 'trace-b')
    assert key('one', 'owner', 'trace-a') != key('two', 'owner', 'trace-a')
    assert key('one', 'owner', 'trace-a') != key('one', 'other', 'trace-a')
