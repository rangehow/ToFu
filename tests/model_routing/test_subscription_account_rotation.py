"""Multi-account subscription pooling: account-expanded candidates,
account-scoped health, and rotation to a sibling account on quota exhaustion
or auth rejection.

A router-selected account binding may rotate; a caller-pinned binding keeps
the terminal contract (``test_codex_account_flow``). One account's outage
never excludes its siblings, and a quota window the provider named becomes
that account's cooldown — not a whole-route penalty.
"""
import copy
import time
from types import SimpleNamespace

import pytest

from lib.llm.llm_errors import ModelAdmissionRefused, PermissionError_, RateLimitError
from lib.model_routing.candidate_compiler import compile_candidates
from lib.model_routing.domain import NativeModelSelection, ProviderOfferingRef
from lib.model_routing.health import (
    HealthTarget, classify_failure, owner_health_registry,
    reset_owner_health_registries,
)
from lib.model_routing.repository import OwnerBoundary, StoredAuthority
from lib.oauth.accounts import AccountBinding
from lib.oauth.codex import account_fingerprint
from tests.model_routing.test_native_route import Repository

pytestmark = pytest.mark.unit

ACCOUNT_A = 'oa_' + 'a' * 32
ACCOUNT_B = 'oa_' + 'b' * 32
SECRET_A = 'oauth_' + 'a' * 32
SECRET_B = 'oauth_' + 'b' * 32


@pytest.fixture(autouse=True)
def _isolated_health():
    from lib.subscription_quota import _reset_subscription_quota_cache_for_tests
    reset_owner_health_registries()
    _reset_subscription_quota_cache_for_tests()
    yield
    reset_owner_health_registries()
    _reset_subscription_quota_cache_for_tests()


def _account_row(account_id, subject, secret, *, default=False, expires_at=None):
    return {
        'provider': 'codex', 'account_id': account_id,
        'subject_fingerprint': account_fingerprint(subject),
        'secret_reference': secret, 'alias': '', 'is_default': default,
        'expires_at': time.time() + 3600 if expires_at is None else expires_at,
    }


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
        account_fingerprint('account-b'): [dict(creator_id='openai', model_id='alpha',
                                               wire_model_id='alpha-wire-a')],
    })
    doc['subscription_accounts'] = [
        _account_row(ACCOUNT_A, 'account-a', SECRET_A, default=True),
        _account_row(ACCOUNT_B, 'account-b', SECRET_B),
    ]
    repo.resolve_secret = lambda *_: 'oauth:codex'

    def compare_and_swap(boundary, document, *, expected_revision, account_mutation=False):
        assert expected_revision == 3
        repo.document = copy.deepcopy(document)
        return SimpleNamespace(revision=4)

    repo.compare_and_swap = compare_and_swap
    selection = NativeModelSelection(None, ProviderOfferingRef('oauth_codex', 'alpha-a'))
    return repo, selection


def test_compiler_expands_authorized_accounts_default_first(scoped_route):
    repo, selection = scoped_route
    candidates = compile_candidates(repo.document, selection)
    assert [c.subscription_account_id for c in candidates] == [ACCOUNT_A, ACCOUNT_B]
    assert all(set(c.codex_account_fingerprints) == {
        account_fingerprint('account-a'), account_fingerprint('account-b')}
        for c in candidates)
    assert candidates[0].selection_reasons[-1] == 'default_account'
    assert candidates[1].selection_reasons[-1] == 'eligible_account'


def test_compiler_skips_expired_account(scoped_route):
    repo, selection = scoped_route
    repo.document['subscription_accounts'][1]['expires_at'] = 1
    candidates = compile_candidates(repo.document, selection)
    assert [c.subscription_account_id for c in candidates] == [ACCOUNT_A]


def test_compiler_skips_cooled_account_and_keeps_sibling(scoped_route):
    repo, selection = scoped_route
    registry = owner_health_registry(7)
    registry.record_failure(HealthTarget('subscription_account', ACCOUNT_A, 'oauth_codex'),
                            reason='subscription_quota_exhausted:429', cooldown_seconds=900)
    candidates = compile_candidates(repo.document, selection, health=registry)
    assert [c.subscription_account_id for c in candidates] == [ACCOUNT_B]


def test_compiler_without_inventory_keeps_legacy_candidate(scoped_route):
    repo, selection = scoped_route
    del repo.document['subscription_accounts']
    candidates = compile_candidates(repo.document, selection)
    assert len(candidates) == 1
    assert candidates[0].subscription_account_id == ''
    assert candidates[0].codex_account_fingerprints  # grants still proven


def test_account_health_scope_and_window():
    assert classify_failure(kind='subscription_quota_exhausted') == 'subscription_account'
    assert classify_failure(kind='subscription_account_rejected') == 'subscription_account'
    registry = owner_health_registry(7)
    candidate = SimpleNamespace(
        subscription_account_id=ACCOUNT_A,
        provider={'provider_id': 'oauth_codex'},
        credential={'credential_id': 'credential-a'},
        deployment={'deployment_id': 'deployment-a'},
        connection={'connection_id': 'connection-a'},
    )
    target = registry.record_candidate_failure(
        candidate, kind='subscription_quota_exhausted', reason='429',
        cooldown_seconds=900)
    assert target == HealthTarget('subscription_account', ACCOUNT_A, 'oauth_codex')
    unavailable, penalty, reasons = registry.candidate_state(candidate)
    assert unavailable and penalty == 1.0
    assert reasons == ['subscription_account:429']
    sibling = SimpleNamespace(**{**candidate.__dict__, 'subscription_account_id': ACCOUNT_B})
    assert registry.candidate_state(sibling) == (False, 0.0, [])
    registry.record_success(candidate)
    assert registry.candidate_state(candidate) == (False, 0.0, [])


def _bound_slot(key, account_id, *, rotation=True, owner=7):
    from lib.llm_dispatch.slot import Slot
    slot = Slot(key_name=key, api_key='oauth', model='gpt-6-astra', capabilities={'text'})
    slot.oauth = 'codex'
    slot.routing_owner_user_id = owner
    slot.routing_tenant_id = ''
    slot.route_credential_id = 'credential-a'
    if account_id:
        binding = AccountBinding(OwnerBoundary.create(owner), 'codex', account_id,
                                 SECRET_A if account_id == ACCOUNT_A else SECRET_B)
        slot.subscription_account = (binding, None)
        slot.subscription_account_rotation = rotation
    return slot


def test_outage_sharing_is_account_aware():
    from lib.llm_dispatch._credential_outage import _shares_credential_source
    a1 = _bound_slot('k1', ACCOUNT_A)
    a2 = _bound_slot('k2', ACCOUNT_A)
    b = _bound_slot('k3', ACCOUNT_B)
    legacy = _bound_slot('k4', '')
    legacy2 = _bound_slot('k5', '')
    assert _shares_credential_source(a2, a1)
    assert not _shares_credential_source(b, a1)
    assert not _shares_credential_source(legacy, a1)
    assert not _shares_credential_source(a1, legacy)
    assert _shares_credential_source(legacy2, legacy)


class _FakeDispatcher:
    def __init__(self, slots):
        self._q = list(slots)
        self.slots = list(slots)

    def pick_and_reserve(self, **kw):
        s = self._q.pop(0) if self._q else None
        if s is not None:
            s.record_request()
        return s

    def has_capable_slots(self, *a, **k):
        return bool(self._q)

    def summarize_slots(self, *a, **k):
        return 'fake'

    def note_shared_contention(self, slot):
        pass


@pytest.fixture
def dispatch_env(monkeypatch):
    from lib.llm_dispatch import api
    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep', lambda *_a, **_k: None)
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
    monkeypatch.setattr('lib.key_stats.record_outcome', lambda *a, **k: None)
    monkeypatch.setattr('lib.key_stats.record_rate_limit', lambda *a, **k: False)

    def install(slots, first_error):
        disp = _FakeDispatcher(slots)
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise first_error
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)
        return disp, calls

    return install


def test_quota_429_rotates_to_sibling_account(dispatch_env):
    from lib.llm_dispatch import api
    slot_a = _bound_slot('k1', ACCOUNT_A)
    slot_b = _bound_slot('k2', ACCOUNT_B)
    disp, calls = dispatch_env(
        [slot_a, slot_b],
        RateLimitError('usage_limit_reached "resets_in_seconds": 900', status_code=429))
    retries = []
    msg, finish, _usage = api.dispatch_stream(
        [{'role': 'user', 'content': 'hi'}], log_prefix='[t]', owner_user_id=7,
        on_retry=lambda **kw: retries.append(kw))
    assert msg == 'ok' and calls['n'] == 2
    assert retries[0]['reason'] == 'Subscription quota reached'
    # The named window becomes the failed account's cooldown, not the route's.
    registry = owner_health_registry(7)
    assert registry.target_unavailable(
        HealthTarget('subscription_account', ACCOUNT_A, 'codex'))
    assert not registry.target_unavailable(
        HealthTarget('subscription_account', ACCOUNT_B, 'codex'))
    assert not registry.target_unavailable(HealthTarget('credential', 'credential-a'))
    entries = {e['entity_id']: e for e in registry.snapshot()['entries']}
    assert 800 < entries[ACCOUNT_A]['cooldown_remaining'] <= 900
    # The sibling account stayed eligible and served the unchanged request.
    assert slot_b.total_errors == 0


def test_auth_401_retires_account_and_rotates(dispatch_env, scoped_route):
    from lib.llm_dispatch import api
    repo, _selection = scoped_route
    slot_a = _bound_slot('k1', ACCOUNT_A)
    slot_a.subscription_account = (AccountBinding(
        repo.boundary, 'codex', ACCOUNT_A, SECRET_A), repo)
    slot_b = _bound_slot('k2', ACCOUNT_B)
    _disp, calls = dispatch_env([slot_a, slot_b], PermissionError_('denied', status_code=401))
    msg, _finish, _usage = api.dispatch_stream(
        [{'role': 'user', 'content': 'hi'}], log_prefix='[t]', owner_user_id=7)
    assert msg == 'ok' and calls['n'] == 2
    retired = next(row for row in repo.document['subscription_accounts']
                   if row['account_id'] == ACCOUNT_A)
    assert retired['expires_at'] == 0
    assert owner_health_registry(7).target_unavailable(
        HealthTarget('subscription_account', ACCOUNT_A, 'codex'))


def _quota_snapshot(used, *, resets_at=None, captured_at=None, minutes=300):
    window = {'used_percent': used, 'remaining_percent': round(100.0 - used, 3),
              'window_minutes': minutes}
    if resets_at is not None:
        window['resets_at'] = resets_at
    return {'provider': 'codex', 'source': 'response_headers',
            'captured_at': int(time.time() if captured_at is None else captured_at),
            'primary': window, 'secondary': None}


def test_scoped_quota_cache_key():
    from lib.subscription_quota import scoped_quota_cache_key
    binding = AccountBinding(OwnerBoundary.create(7), 'codex', ACCOUNT_A, SECRET_A)
    assert scoped_quota_cache_key('oauth_codex', (binding, None)) == f'oauth_codex:{ACCOUNT_A}'
    assert scoped_quota_cache_key('oauth_codex', None) == 'oauth_codex'
    assert scoped_quota_cache_key('codex', (binding, None)) == 'codex'
    assert scoped_quota_cache_key('adapter:agent-1', (binding, None)) == 'adapter:agent-1'


def test_per_account_quota_recording():
    from lib.oauth.account_dispatch import scoped_account_dispatch
    from lib.subscription_quota import latest_subscription_quota, record_codex_quota
    binding = AccountBinding(OwnerBoundary.create(7), 'codex', ACCOUNT_A, SECRET_A)
    slot = SimpleNamespace(subscription_account=(binding, None))
    headers = {'x-codex-primary-used-percent': '42',
               'x-codex-primary-window-minutes': '300'}
    with scoped_account_dispatch(slot):
        from lib.oauth.account_dispatch import current_account_dispatch
        from lib.subscription_quota import scoped_quota_cache_key
        usage = record_codex_quota(
            headers, {}, cache_key=scoped_quota_cache_key(
                'oauth_codex', current_account_dispatch()))
    assert usage['_subscription_quota']['primary']['used_percent'] == 42.0
    mine = latest_subscription_quota(cache_key=f'oauth_codex:{ACCOUNT_A}')
    assert mine is not None and mine['primary']['used_percent'] == 42.0
    assert latest_subscription_quota(cache_key='oauth_codex') is None


def test_nearly_spent_default_yields_to_fresh_sibling(scoped_route):
    repo, selection = scoped_route
    quota = {ACCOUNT_A: _quota_snapshot(95.0, resets_at=int(time.time()) + 3600)}
    candidates = compile_candidates(repo.document, selection, account_quota=quota)
    assert [c.subscription_account_id for c in candidates] == [ACCOUNT_B, ACCOUNT_A]
    assert 'quota_nearly_spent' in candidates[1].selection_reasons


def test_exhausted_account_is_skipped(scoped_route):
    repo, selection = scoped_route
    quota = {ACCOUNT_A: _quota_snapshot(100.0, resets_at=int(time.time()) + 3600)}
    rejections = {}
    candidates = compile_candidates(repo.document, selection,
                                    account_quota=quota, rejections=rejections)
    assert [c.subscription_account_id for c in candidates] == [ACCOUNT_B]
    assert rejections['account_quota_exhausted'] == 1


def test_stale_projection_is_ignored(scoped_route):
    repo, selection = scoped_route
    rolled = {ACCOUNT_A: _quota_snapshot(100.0, resets_at=int(time.time()) - 60)}
    candidates = compile_candidates(repo.document, selection, account_quota=rolled)
    assert [c.subscription_account_id for c in candidates] == [ACCOUNT_A, ACCOUNT_B]
    ancient = {ACCOUNT_A: _quota_snapshot(100.0, captured_at=time.time() - 7200,
                                          minutes=60)}
    candidates = compile_candidates(repo.document, selection, account_quota=ancient)
    assert [c.subscription_account_id for c in candidates] == [ACCOUNT_A, ACCOUNT_B]


def test_pinned_binding_stays_terminal(dispatch_env, scoped_route):
    from lib.llm_dispatch import api
    repo, _selection = scoped_route
    slot_a = _bound_slot('k1', ACCOUNT_A, rotation=False)
    slot_a.subscription_account = (AccountBinding(
        repo.boundary, 'codex', ACCOUNT_A, SECRET_A), repo)
    _disp, calls = dispatch_env([slot_a], PermissionError_('denied', status_code=401))
    with pytest.raises(ModelAdmissionRefused, match='needs reauthorization'):
        api.dispatch_stream([{'role': 'user', 'content': 'hi'}],
                            log_prefix='[t]', owner_user_id=7)
    assert calls['n'] == 1