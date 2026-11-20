"""The aggregate retains detached, account-bound Codex observation history."""
import copy

import pytest

from lib.model_routing.domain import empty_document, normalize_document, ModelRoutingError

pytestmark = pytest.mark.unit


def _entry():
    return {'provider_access_id': 'access', 'credential_id': 'credential',
            'account_fingerprint': 'a' * 64, 'complete': True,
            'wire_model_ids': ['w'], 'models': [{'creator_id': 'openai', 'model_id': 'm'}],
            'pending_wire_model_ids': ['w']}


def test_seen_history_and_revocations_round_trip_without_live_entities():
    document = empty_document()
    document['codex_seen_history'] = [_entry()]
    document['revocation_history'] = [{'credential_id': 'credential', 'creator_id': 'openai',
                                       'model_id': 'm', 'model_revoked': True,
                                       'excluded_wire_model_ids': []}]
    normalized = normalize_document(document)
    assert normalized['codex_seen_history'] == document['codex_seen_history']
    assert normalized['revocation_history'] == document['revocation_history']
    assert normalize_document(normalized) == normalized


@pytest.mark.parametrize('change', [
    {'account_fingerprint': ''}, {'complete': False},
    {'pending_wire_model_ids': ['unseen']}, {'models': [{'creator_id': 'openai'}]},
])
def test_invalid_history_fails_closed(change):
    document = empty_document()
    entry = copy.deepcopy(_entry())
    entry.update(change)
    document['codex_seen_history'] = [entry]
    with pytest.raises(ModelRoutingError):
        normalize_document(document)


def test_duplicate_account_scope_is_refused():
    document = empty_document()
    document['codex_seen_history'] = [_entry(), _entry()]
    with pytest.raises(ModelRoutingError):
        normalize_document(document)
