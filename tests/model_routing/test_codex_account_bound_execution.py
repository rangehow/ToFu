"""An account switch never transfers a compiled automatic Codex model grant."""
import asyncio
from base64 import urlsafe_b64encode
from contextlib import contextmanager
import json
import time
from types import SimpleNamespace

import pytest

from lib.llm.llm_errors import SubscriptionCredentialUnavailableError
from lib.model_routing.candidate_compiler import compile_candidates
from lib.model_routing.dispatch_adapter import dispose_routed_slot_group, mint_routed_slot_group
from lib.model_routing.domain import NativeModelSelection, ProviderOfferingRef
from lib.model_routing.native_route import compile_native_route, run_native_route
from lib.oauth.codex import account_fingerprint
from lib.oauth.outbound import resolve_oauth_request
from tests.model_routing.test_native_route import Repository, principal

pytestmark = pytest.mark.unit


def bearer(account):
    claim = {"https://api.openai.com/auth": {"chatgpt_account_id": account}}
    return "e30." + urlsafe_b64encode(json.dumps(claim).encode()).decode().rstrip("=") + ".synthetic"


def credentials(account, claimed_account=None):
    return {"account_id": account, "access_token": bearer(claimed_account or account),
            "expire": time.time() + 3600, "owner_user_id": 7}


@pytest.fixture
def scoped_route():
    repo = Repository()
    doc = repo.document
    doc['creators'][0]['creator_id'] = 'openai'
    for ref in doc['models'] + [row['model'] for row in doc['offerings']]:
        ref['creator_id'] = 'openai'
    doc['providers'][0]['provider_id'] = 'oauth_codex'
    doc['provider_accesses'][0]['provider_id'] = 'oauth_codex'
    doc['credentials'][1]['enabled'] = False
    doc['credentials'][1]['authorization']['models'] = []
    doc['connections'][0].update(protocol='openai_responses', responses_profile='codex',
                                base_url='https://chatgpt.com/backend-api/codex')
    credential = doc['credentials'][0]
    credential['kind'] = 'oauth'
    credential['authorization'].update(models=[], codex_account_grants={
        account_fingerprint('account-a'): [dict(creator_id='openai', model_id='alpha',
                                               wire_model_id='alpha-wire-a')],
    })
    repo.resolve_secret = lambda *_: 'oauth:codex'
    return repo, NativeModelSelection(None, ProviderOfferingRef('oauth_codex', 'alpha-a'))


@pytest.mark.parametrize('blocked_by', ['wire', 'whole_revocation', 'wire_revocation', 'manual_exclusion'])
def test_compiler_requires_exact_route_and_respects_owner_denials(scoped_route, blocked_by):
    repo, selection = scoped_route
    assert compile_candidates(repo.document, selection)[0].codex_account_fingerprints == (
        account_fingerprint('account-a'),)
    if blocked_by == 'wire':
        original = repo.document['deployments'][0]
        repo.document['deployments'].append({**original, 'deployment_id': 'alias-deployment',
                                           'wire_model_id': 'new-alias'})
        assert [row.deployment['wire_model_id'] for row in compile_candidates(repo.document, selection)] == ['alpha-wire-a']
        return
    elif blocked_by == 'manual_exclusion':
        repo.document['credentials'][0]['authorization']['models'] = [dict(
            creator_id='openai', model_id='alpha', excluded_wire_model_ids=['alpha-wire-a'])]
    else:
        repo.document['revocation_history'] = [dict(
            credential_id='credential-a', creator_id='openai', model_id='alpha',
            model_revoked=blocked_by == 'whole_revocation',
            excluded_wire_model_ids=['alpha-wire-a'] if blocked_by == 'wire_revocation' else [])]
    assert compile_candidates(repo.document, selection) == []


def test_pending_managed_offering_without_any_grants_is_not_callable(scoped_route):
    repo, selection = scoped_route
    offering = repo.document['offerings'][0]
    offering.pop('model')
    offering.update(identity_state='pending_identity', pending_model_id='alpha-wire-a')
    repo.document['credentials'][0]['authorization'].pop('codex_account_grants')
    assert compile_candidates(repo.document, selection) == []


def test_manual_grant_is_not_scoped(scoped_route):
    repo, selection = scoped_route
    repo.document['credentials'][0]['authorization']['models'] = [dict(
        creator_id='openai', model_id='alpha')]
    assert compile_candidates(repo.document, selection)[0].codex_account_fingerprints == ()


@pytest.mark.parametrize('first_account', ['account-a', 'account-b'])
def test_outbound_switch_during_resolution_never_mixes_bearer_and_account(monkeypatch, first_account):
    from lib.oauth import codex
    reads = []

    def switched_store(provider):
        reads.append(provider)
        # A switched store would claim A regardless of what the attempt has read.
        return credentials(first_account if len(reads) == 1 else 'account-a')

    monkeypatch.setattr(codex, 'load_token', switched_store)
    monkeypatch.setattr('lib.oauth.token_store.load_token', switched_store)
    options = dict(codex_account_fingerprints=(account_fingerprint('account-a'),))
    if first_account == 'account-b':
        with pytest.raises(SubscriptionCredentialUnavailableError):
            resolve_oauth_request('codex', {}, None, **options)
    else:
        token, headers, _ = resolve_oauth_request('codex', {}, {
            'authorization': 'Bearer overridden', 'ChatGPT-Account-ID': 'account-b'}, **options)
        assert token == bearer('account-a')
        assert headers['chatgpt-account-id'] == 'account-a'
        assert [key for key in headers if key.lower() == 'chatgpt-account-id'] == ['chatgpt-account-id']
        assert not any(key.lower() == 'authorization' for key in headers)
    assert reads == ['codex']


@pytest.mark.parametrize('replacement', ['changed', 'inconsistent', 'unproven'])
def test_refreshed_credential_must_prove_the_grant(monkeypatch, replacement):
    from lib.oauth import codex
    expired = dict(credentials('account-a'), expire=1, refresh_token='first-refresh')
    refreshed = {'changed': credentials('account-b'),
                 'inconsistent': credentials('account-a', 'account-b'),
                 'unproven': dict(credentials('account-a'), access_token='opaque')}[replacement]
    monkeypatch.setattr(codex, 'load_token', lambda _: expired)
    monkeypatch.setattr(codex, 'codex_refresh_token', lambda *_args, **_kwargs: refreshed)
    with pytest.raises(SubscriptionCredentialUnavailableError):
        resolve_oauth_request('codex', {}, None,
                              codex_account_fingerprints=(account_fingerprint('account-a'),))


@pytest.mark.parametrize('mode', ['sync', 'async', 'complete_text'])
def test_minted_dispatch_refuses_switched_account_before_provider_ingress(
        scoped_route, monkeypatch, mode):
    from lib.llm_dispatch import api
    from lib.llm_dispatch.provider_pin import provider_pin
    from lib.oauth import codex
    repo, selection = scoped_route
    monkeypatch.setenv('TOFU_DISABLE_CONFIGURED_SLOTS', '1')
    # Avoid endpoint DNS and live credentials; the account switches *after* mint.
    from lib.llm_dispatch import ephemeral
    monkeypatch.setattr(ephemeral, '_check_endpoint',
                        lambda base_url: ephemeral._EndpointVerdict(base_url, False))
    state = credentials('account-a')
    monkeypatch.setattr(codex, 'load_token', lambda _: dict(state))

    def unexpected_ingress(*args, **kwargs):
        pytest.fail('switched account reached provider transport')

    monkeypatch.setattr('lib.llm.stream.get_sync_session', unexpected_ingress)
    monkeypatch.setattr('lib.llm.astream.get_async_client', unexpected_ingress)
    group = mint_routed_slot_group(repo, repo.boundary, selection)
    try:
        assert group.handles[0].slot.codex_account_fingerprints == (account_fingerprint('account-a'),)
        state.update(credentials('account-b'))
        messages = [{'role': 'user', 'content': 'fixture'}]
        with provider_pin(group.pin_id), pytest.raises(Exception) as error:
            options = dict(prefer_model='alpha', strict_model=True, owner_user_id=7, max_retries=1)
            if mode == 'sync':
                api.dispatch_stream(messages, **options)
            elif mode == 'async':
                asyncio.run(api.async_dispatch_stream(messages, **options))
            else:
                api.dispatch_chat(messages, **options)
        from lib.llm.llm_errors import LocalRequestPreparationError
        assert isinstance(error.value, (SubscriptionCredentialUnavailableError, LocalRequestPreparationError))
        refusal = error.value.__cause__ if isinstance(error.value, LocalRequestPreparationError) else error.value
        assert isinstance(refusal, SubscriptionCredentialUnavailableError)
        assert refusal.reason == 'account_changed'
    finally:
        dispose_routed_slot_group(group)


def test_native_route_checks_exact_callback_headers_before_returning(scoped_route, tmp_path):
    repo, selection = scoped_route
    account = 'account-a'

    def resolve(_request):
        # Simulate a resolver supplying a stale account header after a switch.
        return {'Authorization': 'Bearer ' + bearer(account), 'chatgpt-account-id': 'account-a'}

    class Policy:
        @contextmanager
        def bind_credentials(self, *, resolver, **kwargs):
            self.resolve = resolver
            yield 'fixture-cache'

    policy = Policy()
    sent = []

    async def run(configuration, **_kwargs):
        nonlocal account
        assert 'codex_account_fingerprints' not in configuration
        sent.append(policy.resolve(None))
        account = 'account-b'
        with pytest.raises(SubscriptionCredentialUnavailableError) as error:
            policy.resolve(None)
        assert error.value.reason == 'account_changed'
        return {"status": "done"}

    asyncio.run(run_native_route(SimpleNamespace(principal=principal(), run=run),
        compile_native_route(repo, repo.boundary, selection, credential_resolver=resolve),
        {'owner_user_id': 7, 'task_id': 'fixture'},
        prepare_template=lambda route: {'model': route['wire_model'], 'max_tokens': 128},
        callbacks={}, working_directory=tmp_path, model_policy=policy))
    assert [row['Authorization'] for row in sent] == ['Bearer ' + bearer('account-a')]
