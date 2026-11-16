#!/usr/bin/env python3
"""tests/error_envelope/test_billing_stop_envelope.py — typed attribution of a
provider billing stop (HTTP 402, or a quota-exhausted 429).

The dispatcher's failure policy is the only stage that knows which credential
the provider refused. A Turn's ``routeSnapshot`` / ``lastRoundUsage`` record the
last SUCCESSFUL round, so after a failover they name a DIFFERENT key than the
one that ran out of credit (2026-11-09 Worker turn 411d2578, where the failing
key was attributed to the round-246 key). The refused route therefore travels
as the typed ``billingStop`` envelope field, carrying public identifiers and the
already-public key TAIL only — never key material or a guessed AppId.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]


def test_dispatch_402_carries_the_refused_route_without_key_material():
    """Real slot + real failure policy: the 402's own route is the evidence."""
    from lib.error_envelope import from_exception
    from lib.llm.llm_errors import RateLimitError
    from lib.llm_dispatch._api_failures import DispatchFailurePolicy
    from lib.llm_dispatch._api_retry_state import DispatchRetryState
    from lib.llm_dispatch.slot import Slot

    slot = Slot(key_name='ephemeral-synthetic', api_key='synthetic-secret-key-value',
                model='kimi-k3.1-pro', capabilities={'text'}, provider_id='route-group-x',
                routing_provider_id='default', routing_owner_user_id=1,
                route_credential_id='cred-synthetic', route_deployment_id='dep-synthetic')
    policy = DispatchFailurePolicy(DispatchRetryState(), None, capability='text',
                                   strict_model=True, max_429_attempts=3,
                                   owner_user_id=1, log_prefix='[test]')
    error = RateLimitError('API HTTP 402: credit exhausted', is_quota=True, status_code=402)
    policy._rate_limit(error, slot, '[test]')

    envelope = from_exception(error)
    evidence = envelope['billingStop']
    assert evidence['providerId'] == 'example-corp', 'public Provider id, not the route group'
    assert evidence['credentialId'] == 'cred-synthetic'
    assert evidence['deploymentId'] == 'dep-synthetic'
    assert evidence['model'] == 'kimi-k3.1-pro'
    assert evidence['statusCode'] == 402
    assert evidence['scope'] == 'key', 'HTTP 402 is the account credit pool'
    assert evidence['keyTail'] == 'alue'
    assert evidence['observedAtMs'] > 0
    assert 'synthetic-secret-key-value' not in str(envelope)
    assert envelope['statusCode'] == 402, 'the existing status field is preserved'
    assert slot.key_name == 'ephemeral-synthetic' and 'ephemeral-synthetic' not in str(evidence), (
        'the request-random slot handle is not the credential identity')


def test_quota_429_evidence_is_model_scoped():
    from lib.llm.llm_errors import RateLimitError
    from lib.llm_dispatch._api_failures import DispatchFailurePolicy
    from lib.llm_dispatch._api_retry_state import DispatchRetryState
    from lib.llm_dispatch.slot import Slot

    slot = Slot(key_name='k', api_key='another-synthetic-secret', model='qwen3.5-plus',
                capabilities={'text'}, provider_id='p', routing_provider_id='gw',
                route_credential_id='cred-q', route_deployment_id='dep-q')
    policy = DispatchFailurePolicy(DispatchRetryState(), None, capability='text',
                                   strict_model=True, max_429_attempts=3,
                                   owner_user_id=1, log_prefix='[test]')
    error = RateLimitError('insufficient_quota', is_quota=True, status_code=429)
    policy._rate_limit(error, slot, '[test]')
    evidence = error.billing_stop
    assert evidence['scope'] == 'model', 'an ambiguous vendor-quota 429 is not account-wide'
    assert evidence['statusCode'] == 429
    assert evidence['keyTail'] == 'cret'


def test_billing_stop_is_declared_in_the_openapi_envelope_schema():
    from lib.error_envelope import typed_error_envelope_schema

    properties = typed_error_envelope_schema()['properties']
    stop = properties['billingStop']
    assert stop['properties']['keyTail']['maxLength'] == 4
    assert set(stop['properties']) == {
        'providerId', 'credentialId', 'deploymentId', 'model',
        'keyTail', 'statusCode', 'scope', 'observedAtMs'}
    assert stop['additionalProperties'] is False


def test_an_error_without_a_billing_stop_keeps_its_shape():
    from lib.error_envelope import from_exception
    from lib.llm.llm_errors import RateLimitError

    envelope = from_exception(RateLimitError('API HTTP 429: too many requests', status_code=429))
    assert 'billingStop' not in envelope, 'ordinary 429 contention gains no billing evidence'
    assert envelope['statusCode'] == 429
