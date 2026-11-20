"""Selected SQL transaction fences for private account secret generations."""
import pytest

from lib.storage.errors import StorageError
from tests.storage_authority_sql.test_model_routing_secret_fences import (
    OWNER, command, secret_payload, sql_backend,
)
__all__ = ['sql_backend']

pytestmark = pytest.mark.unit


def test_private_account_reference_is_required_and_retained(sql_backend):
    ref = 'oauth_' + 'a' * 32
    document = {'contract_version': 'tofu.model-routing/v2', 'revision': 1,
                'subscription_accounts': [{'secret_reference': ref}]}
    payload = {**OWNER, 'expected_revision': 0, 'document': document, 'updated_at': 2.0}
    with pytest.raises(StorageError, match='Subscription account secret unavailable'):
        command(sql_backend, 'model_routing.commit', payload)
    command(sql_backend, 'model_routing.secret.put', {**secret_payload(reference=ref), 'owner_user_id': 8})
    with pytest.raises(StorageError, match='Subscription account secret unavailable'):
        command(sql_backend, 'model_routing.commit', payload)
    command(sql_backend, 'model_routing.secret.put', secret_payload(reference=ref))
    assert command(sql_backend, 'model_routing.commit', payload)['revision'] == 1
    assert not command(sql_backend, 'model_routing.secret.delete', {**OWNER, 'secret_reference': ref})['deleted']
    assert command(sql_backend, 'model_routing.secret.prune', {
        **OWNER, 'active_secret_references': [], 'updated_before': 100.0})['count'] == 0
    document.update(revision=2, subscription_accounts=[])
    payload['expected_revision'] = 1
    assert command(sql_backend, 'model_routing.commit', payload)['revision'] == 2
    assert command(sql_backend, 'model_routing.secret.delete', {**OWNER, 'secret_reference': ref})['deleted']
