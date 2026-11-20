"""Synthetic account management over the owner/tenant revisioned vault port."""
import pytest

from lib.model_routing.domain import ModelRoutingError
from lib.model_routing.repository import InMemoryModelRoutingRepository, OwnerBoundary
from lib.oauth.accounts import SubscriptionAccounts

pytestmark = pytest.mark.unit


def test_two_accounts_default_rename_reauth_remove_and_concurrent_intent():
    repo = InMemoryModelRoutingRepository()
    service = SubscriptionAccounts(repo)
    owner = OwnerBoundary.create(1, 'fixture')
    provider = 'codex'

    def commit(intent, subject, value):
        return service.complete(intent, verified_subject=subject, token={'access_token': value},
                                expires_at=32_503_679_999)

    pending_a = service.begin(owner, provider, 0)
    stale = service.begin(owner, provider, 0)
    a = commit(pending_a, 'subject-a', 'value-a')
    with pytest.raises(ModelRoutingError, match='changed'):
        commit(stale, 'subject-b', 'value-b')
    b = commit(service.begin(owner, provider, 1), 'subject-b', 'value-b')
    assert service.inventory(owner, provider)['default_account_id'] is None
    with pytest.raises(ModelRoutingError, match='unavailable'):
        service.bind(owner, provider, None)
    assert service.update(owner, provider, a, 2, action='default') == 3
    selected_a = service.bind(owner, provider, None)
    assert service.update(owner, provider, a, 3, action='rename', alias='My Alias') == 4
    assert service.update(owner, provider, b, 4, action='default') == 5
    assert service.resolve(selected_a) == {'access_token': 'value-a'}
    assert service.inventory(owner, provider) == {
        'provider': provider, 'revision': 5, 'default_account_id': b,
        'accounts': sorted([{'account_id': a, 'alias': 'My Alias', 'status': 'authorized'},
                            {'account_id': b, 'alias': '', 'status': 'authorized'}], key=lambda x: x['account_id']),
    }
    assert not service.inventory(OwnerBoundary.create(1, 'other'), provider)['accounts']
    assert not service.inventory(OwnerBoundary.create(2, 'fixture'), provider)['accounts']
    with pytest.raises(ModelRoutingError, match='unavailable'):
        service.begin(owner, 'claude', 5, account_id=a)
    reauth = service.begin(owner, provider, 5, account_id=a)
    with pytest.raises(ModelRoutingError, match='unavailable'):
        commit(reauth, 'subject-b', 'wrong')
    assert commit(reauth, 'subject-a', 'reauth-a') == a
    with pytest.raises(ModelRoutingError, match='unavailable'):
        service.resolve(selected_a)
    selected_b = service.bind(owner, provider, None)
    assert service.update(owner, provider, b, 6, action='remove') == 7
    assert service.inventory(owner, provider)['default_account_id'] is None
    with pytest.raises(ModelRoutingError, match='unavailable'):
        service.resolve(selected_b)
    with pytest.raises(ModelRoutingError, match='changed'):
        commit(reauth, 'subject-a', 'stale')
    assert service.resolve(service.bind(owner, provider, a)) == {'access_token': 'reauth-a'}


def test_generic_routing_cas_reveal_and_cleanup_do_not_cross_vault_boundary():
    repo = InMemoryModelRoutingRepository()
    service = SubscriptionAccounts(repo)
    owner = OwnerBoundary.create(1, 'fixture')
    a = service.complete(service.begin(owner, 'codex', 0), verified_subject='subject',
                         token={'access_token': 'synthetic'}, expires_at=32_503_679_999)
    binding = service.bind(owner, 'codex', a)
    row = repo.get(owner)
    assert 'subscription_accounts' not in row.public_document()
    assert repo.secret_metadata(owner) == []
    with pytest.raises(ModelRoutingError, match='not available'):
        repo.resolve_secret(owner, binding.secret_reference)
    with pytest.raises(ModelRoutingError, match='immutable'):
        repo.put_secret(owner, 'replacement', secret_reference=binding.secret_reference)
    assert not repo.delete_secret(owner, binding.secret_reference)
    with pytest.raises(ModelRoutingError, match='lifecycle service'):
        repo.compare_and_swap(owner, {**row.document, 'subscription_accounts': []}, expected_revision=1)
    repo.compare_and_swap(owner, row.public_document(), expected_revision=1)
    assert service.resolve(binding) == {'access_token': 'synthetic'}


def test_revocation_during_decrypt_does_not_return_bearer(monkeypatch):
    repo = InMemoryModelRoutingRepository()
    service = SubscriptionAccounts(repo)
    owner = OwnerBoundary.create(1)
    account = service.complete(service.begin(owner, 'codex', 0), verified_subject='fixture',
                               token={'access_token': 'synthetic'}, expires_at=32_503_679_999)
    binding = service.bind(owner, 'codex', account)
    resolve = repo.resolve_secret

    def read_then_revoke(*args, **kwargs):
        value = resolve(*args, **kwargs)
        service.update(owner, 'codex', account, 1, action='remove')
        return value

    monkeypatch.setattr(repo, 'resolve_secret', read_then_revoke)
    with pytest.raises(ModelRoutingError, match='unavailable'):
        service.resolve(binding)
