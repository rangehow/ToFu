"""Owner document replacement cannot forge automatic Codex grant provenance."""
import copy

import pytest

from lib.model_routing import ModelRoutingError
from lib.model_routing.revocation_history import record_explicit_revocations

pytestmark = pytest.mark.unit

FIELD = 'codex_account_grants'
ACCOUNT = 'a' * 64
GRANT = {'creator_id': 'openai', 'model_id': 'codex-model', 'wire_model_id': 'codex-wire'}


def document(grants, *, access='codex-access'):
    authorization = {'models': []}
    if grants is not None:
        authorization[FIELD] = copy.deepcopy(grants)
    return {'credentials': [{'credential_id': 'c', 'provider_access_id': access,
                             'authorization': authorization}]}


def test_legacy_edit_preserves_automatic_grants():
    previous = document({ACCOUNT: [GRANT]})
    candidate = document(None)
    record_explicit_revocations(previous, candidate)
    assert candidate['credentials'] == previous['credentials']
    assert candidate['revocation_history'] == []


@pytest.mark.parametrize('candidate', [document({ACCOUNT: [GRANT, {**GRANT, 'wire_model_id': 'forged'}]}),
                                       document({'b' * 64: [GRANT]}),
                                       document({ACCOUNT: [GRANT]}, access='another-access')])
def test_owner_replacement_cannot_mint_or_move_grant(candidate):
    with pytest.raises(ModelRoutingError, match='only be narrowed'):
        record_explicit_revocations(document({ACCOUNT: [GRANT]}), copy.deepcopy(candidate))


@pytest.mark.parametrize('candidate', [document({}), {'credentials': []}])
def test_explicit_removal_persists_wire_denial(candidate):
    candidate = copy.deepcopy(candidate)
    record_explicit_revocations(document({ACCOUNT: [GRANT]}), candidate)
    assert candidate['revocation_history'] == [
        {'credential_id': 'c', 'creator_id': 'openai', 'model_id': 'codex-model',
         'model_revoked': False, 'excluded_wire_model_ids': ['codex-wire']}]
