"""Explicit grant edits are distinct from resource and automation changes."""
import copy

import pytest

from lib.model_routing.revocation_history import record_explicit_revocations

pytestmark = pytest.mark.unit


def document(models, history=None):
    result = {'credentials': [{'credential_id': 'key', 'authorization': {'models': models}}]}
    if history is not None:
        result['revocation_history'] = history
    return result


def test_exclusions_regrant_and_untrusted_history():
    grant = {'creator_id': 'creator', 'model_id': 'model'}
    old = document([grant])
    new = document([{**grant, 'excluded_wire_model_ids': ['wire']}], [{'forged': True}])
    record_explicit_revocations(old, new)
    expected = {'credential_id': 'key', **grant, 'model_revoked': False,
                'excluded_wire_model_ids': ['wire']}
    assert new['revocation_history'] == [expected]
    before = copy.deepcopy(new)
    new['credentials'][0]['authorization']['models'] = []
    record_explicit_revocations(before, new)
    assert new['revocation_history'] == [{**expected, 'model_revoked': True}]
    before = copy.deepcopy(new)
    new['credentials'][0]['authorization']['models'] = [grant]
    record_explicit_revocations(before, new)
    assert new['revocation_history'] == []


def test_removed_credential_does_not_erase_or_invent_history():
    entry = {'credential_id': 'key', 'creator_id': 'creator', 'model_id': 'model',
             'model_revoked': True, 'excluded_wire_model_ids': []}
    old = document([], [entry])
    new = {'credentials': [], 'revocation_history': []}
    record_explicit_revocations(old, new)
    assert new['revocation_history'] == [entry]


def test_seen_history_is_server_owned_and_removed_reachability_cancels_reservations():
    old = {'credentials': [{'credential_id': 'key', 'provider_access_id': 'access',
                            'authorization': {'models': [], 'connection_ids': ['conn']}}],
           'offerings': [{'offering_id': 'off', 'provider_access_id': 'access'}],
           'deployments': [{'offering_id': 'off', 'connection_id': 'conn', 'wire_model_id': 'w'}],
           'codex_seen_history': [{'provider_access_id': 'access', 'credential_id': 'key',
                                   'account_fingerprint': 'a' * 64, 'complete': True,
                                   'wire_model_ids': ['w'], 'models': [], 'pending_wire_model_ids': ['w']}]}
    new = copy.deepcopy(old)
    new['codex_seen_history'] = []
    record_explicit_revocations(old, new)
    assert new['codex_seen_history'] == old['codex_seen_history']
    new['credentials'][0]['authorization']['connection_ids'] = []
    record_explicit_revocations(old, new)
    assert new['codex_seen_history'][0]['pending_wire_model_ids'] == []
    assert new['codex_seen_history'][0]['wire_model_ids'] == ['w']
