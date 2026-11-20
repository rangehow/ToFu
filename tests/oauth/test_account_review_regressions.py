import time

import pytest

from lib.model_routing.domain import ModelRoutingError
from lib.model_routing.repository import InMemoryModelRoutingRepository, OwnerBoundary
from lib.oauth.account_dispatch import reject_bound_account
from lib.oauth.accounts import SubscriptionAccounts
from lib.oauth.manager import account_flow

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('race', ['default', 'remove', 'reauth'])
def test_rejection_retries_only_same_generation(monkeypatch, race):
    repo = InMemoryModelRoutingRepository()
    service = SubscriptionAccounts(repo)
    boundary = OwnerBoundary.create(7, 'fixture')
    def link(revision, subject, account_id=None):
        return service.complete(service.begin(boundary, 'codex', revision, account_id=account_id),
                                verified_subject=subject, token={'fixture': True}, expires_at=time.time() + 60)
    a, b = link(0, 'A'), link(1, 'B')
    scoped = service.bind(boundary, 'codex', a), repo
    commit = repo.compare_and_swap
    def conflict(*args, **kwargs):
        monkeypatch.setattr(repo, 'compare_and_swap', commit)
        if race == 'reauth':
            link(2, 'A', a)
        else:
            service.update(boundary, 'codex', b if race == 'default' else a, 2, action=race)
        return commit(*args, **kwargs)
    monkeypatch.setattr(repo, 'compare_and_swap', conflict)
    reject_bound_account(scoped)
    inventory = service.inventory(boundary, 'codex')
    status = {row['account_id']: row['status'] for row in inventory['accounts']}
    assert status[b] == 'authorized'
    assert status.get(a) == {'default': 'needs_reauth', 'remove': None, 'reauth': 'authorized'}[race]
    with pytest.raises(ModelRoutingError):
        service.resolve(scoped[0])


def test_expired_flow_lookup_does_not_delete(monkeypatch):
    from lib.oauth.manager._state import _active_flows
    boundary = OwnerBoundary.create(7, 'fixture')
    key = account_flow._key(boundary)
    expired = {'flow_id': 'fixture', 'expires_at': -1}
    monkeypatch.setitem(_active_flows, key, expired)
    with pytest.raises(ModelRoutingError, match='unavailable'):
        account_flow.status(boundary, 'fixture')
    assert _active_flows[key] is expired
