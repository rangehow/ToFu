"""Owner baseline, account-bound catalog evidence, admission and CAS settlement."""
import base64
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from lib.model_routing import InMemoryModelRoutingRepository, ModelRoutingError, OwnerBoundary
from lib.oauth import codex, codex_catalog, outbound
from lib.oauth.codex_baseline import disable_owner_codex_registration, establish_owner_codex_baseline

pytestmark = pytest.mark.unit
ACCOUNT = 'test-account'
FINGERPRINT = hashlib.sha256(ACCOUNT.encode()).hexdigest()
BASE = 'gpt-5.5'
NEW = 'gpt-6-astra'
PENDING = 'gpt-9-test-codex-registration'


def _bearer(account):
    auth = {'https://api.openai.com/auth': {'chatgpt_account_id': account}}
    claims = base64.urlsafe_b64encode(json.dumps(auth).encode()).rstrip(b'=').decode()
    return f'header.{claims}.signature'


def _response(wires):
    payload = {'models': [{'slug': wire, 'visibility': 'list', 'input_modalities': ['text'],
                           'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'high'}],
                           'default_reasoning_level': 'low'} for wire in wires]}
    return SimpleNamespace(status_code=200, headers={'etag': 'catalog-etag'},
                           content=b'', json=lambda: payload)


@pytest.fixture
def registration(tmp_path, monkeypatch):
    boundary = OwnerBoundary.create(1)
    repo = InMemoryModelRoutingRepository()
    token = {'access_token': _bearer(ACCOUNT), 'account_id': ACCOUNT}
    state = {'wires': [BASE], 'headers': [], 'route': 'direct', 'not_modified': False}
    monkeypatch.setattr('runtime_guards.load_deployment_configuration', lambda: SimpleNamespace(mode='personal'))
    monkeypatch.setattr(codex_catalog, '_cache_path', lambda: str(tmp_path / 'catalog.json'))
    monkeypatch.setattr(codex_catalog, 'codex_client_version', lambda: '1.0.0')
    monkeypatch.setattr(codex_catalog, '_pending_catalog', None)
    monkeypatch.setattr(codex_catalog, '_registration_authority', repo.get)
    monkeypatch.setattr('lib.oauth.token_store.load_token', lambda _name: token)
    monkeypatch.setattr(codex, 'codex_get_valid_token_snapshot', lambda **kw: codex._access_snapshot(token))
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **kw: state['route'])
    monkeypatch.setattr('lib.desktop.egress.egress_http', lambda *a, **kw: _response(state['wires']))
    monkeypatch.setattr('lib.llm_dispatch.reset_dispatcher', lambda: None)

    def http_get(url, **kwargs):
        assert url == 'https://chatgpt.com/backend-api/codex/models?client_version=1.0.0'
        assert kwargs['allow_redirects'] is False
        assert kwargs['headers']['Authorization'] == f"Bearer {token['access_token']}"
        state['headers'].append(kwargs['headers'])
        if state['not_modified']:
            assert 'If-None-Match' in kwargs['headers']
            return SimpleNamespace(status_code=304)
        return _response(state['wires'])

    def provision(*, boundary):
        return outbound.provision_oauth_provider('codex', owner_user_id=boundary.owner_user_id,
                                                tenant_id=boundary.tenant_id, repository=repo)

    monkeypatch.setattr('lib.http_transport.client.http_get', http_get)
    monkeypatch.setattr(codex_catalog, '_provision_from_best_available', provision)

    def refresh(**kwargs):
        result = codex_catalog.refresh_codex_model_catalog(force=True, boundary=boundary, **kwargs)
        assert result['ok'], result
        return repo.get(boundary)

    return SimpleNamespace(repo=repo, boundary=boundary, token=token, state=state, refresh=refresh)


def _opt_in(reg):
    current = reg.repo.get(reg.boundary)
    return establish_owner_codex_baseline(reg.repo, reg.boundary, expected_revision=current.revision)


def _auth(authority):
    return authority.document['credentials'][0]['authorization']


def test_verified_new_supply_grants_only_one_account_and_wire(registration):
    reg = registration
    first = reg.refresh()
    baseline = _opt_in(reg)
    assert 'If-None-Match' not in reg.state['headers'][-1]
    assert _auth(baseline) == _auth(first)
    assert baseline.document['codex_seen_history'][0]['wire_model_ids'] == [BASE]

    reg.state['wires'].append(NEW)
    result = reg.refresh()
    assert _auth(result)['models'] == _auth(first)['models']
    assert _auth(result)['codex_account_grants'] == {FINGERPRINT: [
        {'creator_id': 'openai', 'model_id': NEW, 'wire_model_id': NEW}]}
    assert set(result.document['codex_seen_history'][0]['wire_model_ids']) == {BASE, NEW}
    assert reg.refresh().revision == result.revision


def test_pending_waits_for_ready_binding_and_keeps_offering_id(registration):
    reg = registration
    reg.refresh()
    _opt_in(reg)
    reg.state['wires'].append(PENDING)
    discovered = reg.refresh()
    history = discovered.document['codex_seen_history'][0]
    assert history['pending_wire_model_ids'] == [PENDING]
    offering = next(r for r in discovered.document['offerings'] if r.get('pending_model_id') == PENDING)
    assert 'codex_account_grants' not in _auth(discovered)
    assert _opt_in(reg).document['codex_seen_history'][0] == history

    doc = copy.deepcopy(discovered.document)
    template = next(r for r in doc['models'] if r['model_id'] == BASE)
    model = dict(template, model_id=PENDING, display_name=PENDING,
                 list_pricing={'input': 1, 'output': 1, 'unit': 'per_million_tokens', 'currency': 'USD'},
                 registration_metadata={'release_date': '2026-11-01', 'checked_at': '2026-11-18',
                                        'source_urls': ['https://developers.openai.com/api/docs/models']},
                 inference_profile={'reasoning': {'support': 'supported', 'wire_format': 'reasoning_effort',
                                                 'supported_efforts': ['low', 'high'], 'default_effort': 'low',
                                                 'can_disable': False}}, registration_state='pending_metadata')
    doc['models'].append(model)
    reg.repo.compare_and_swap(reg.boundary, doc, expected_revision=discovered.revision)
    result = reg.refresh()
    assert 'If-None-Match' not in reg.state['headers'][-1]
    bound = next(r for r in result.document['offerings'] if r['offering_id'] == offering['offering_id'])
    assert bound['identity_state'] == 'confirmed'
    assert bound['model'] == {'creator_id': 'openai', 'model_id': PENDING}
    assert _auth(result)['codex_account_grants'][FINGERPRINT] == [
        {'creator_id': 'openai', 'model_id': PENDING, 'wire_model_id': PENDING}]
    assert result.document['codex_seen_history'][0]['pending_wire_model_ids'] == []


@pytest.mark.parametrize('case', ['wrong_bearer', 'lossy_response', 'not_opted_in', 'opted_out', 'revoked', 'wrong_revision'])
def test_unproven_or_revoked_supply_does_not_grant(registration, monkeypatch, case):
    reg = registration
    first = reg.refresh()
    current = _opt_in(reg) if case != 'not_opted_in' else first
    if case == 'wrong_bearer':
        reg.token['access_token'] = _bearer('other-account')
    elif case == 'lossy_response':
        reg.state['wires'].append(BASE)
    elif case == 'opted_out':
        current = disable_owner_codex_registration(reg.repo, reg.boundary, expected_revision=current.revision)
        assert not current.document['codex_seen_history'][0]['complete']
    elif case == 'revoked':
        doc = copy.deepcopy(current.document)
        doc['revocation_history'] = [{'credential_id': 'retired', 'creator_id': 'openai', 'model_id': NEW,
                                      'model_revoked': True, 'excluded_wire_model_ids': []}]
        current = reg.repo.compare_and_swap(reg.boundary, doc, expected_revision=current.revision)
    elif case == 'wrong_revision':
        http_get = codex_catalog._fetch_catalog

        def concurrent_write(*args, **kwargs):
            doc = copy.deepcopy(current.document)
            doc['provider_accesses'][0]['display_name'] = 'owner edit during fetch'
            reg.repo.compare_and_swap(reg.boundary, doc, expected_revision=current.revision)
            return http_get(*args, **kwargs)

        monkeypatch.setattr(codex_catalog, '_fetch_catalog', concurrent_write)
    reg.state['wires'].append(NEW)
    after = reg.refresh()
    assert _auth(after) == _auth(first)
    if case != 'revoked':
        assert after.document.get('codex_seen_history') == current.document.get('codex_seen_history')
    if case == 'wrong_revision':
        monkeypatch.setattr(codex_catalog, '_fetch_catalog', http_get)
        assert _auth(reg.refresh())['codex_account_grants'][FINGERPRINT][0]['wire_model_id'] == NEW
        assert 'If-None-Match' not in reg.state['headers'][-1]


def test_relay_display_and_not_modified_response_cannot_advance_history(registration):
    reg = registration
    reg.refresh()
    baseline = _opt_in(reg)
    reg.state['not_modified'] = True
    assert reg.refresh().revision == baseline.revision

    reg.state.update(not_modified=False, route='desktop-device')
    reg.state['wires'].append(NEW)
    relayed = reg.refresh()
    assert any(row['model_id'] == NEW for row in relayed.document['models'])
    assert _auth(relayed) == _auth(baseline)
    assert relayed.document['codex_seen_history'] == baseline.document['codex_seen_history']

    reg.state['route'] = 'direct'
    direct = reg.refresh()
    assert 'If-None-Match' not in reg.state['headers'][-1]
    assert _auth(direct)['codex_account_grants'][FINGERPRINT][0]['wire_model_id'] == NEW


def test_opt_out_cancels_pending_and_retains_issued_grants(registration):
    reg = registration
    reg.refresh()
    _opt_in(reg)
    reg.state['wires'].extend([NEW, PENDING])
    before = reg.refresh()
    assert before.document['codex_seen_history'][0]['pending_wire_model_ids'] == [PENDING]
    after = disable_owner_codex_registration(reg.repo, reg.boundary, expected_revision=before.revision)
    assert _auth(after)['codex_account_grants'] == _auth(before)['codex_account_grants']
    assert not after.document['codex_seen_history'][0]['complete']
    assert after.document['codex_seen_history'][0]['pending_wire_model_ids'] == []
    assert _opt_in(reg).document['codex_seen_history'][0]['pending_wire_model_ids'] == []


def test_relogin_does_not_recreate_automatic_grant_as_unscoped(registration):
    reg = registration
    reg.refresh()
    _opt_in(reg)
    reg.state['wires'].extend([NEW, PENDING])
    before = reg.refresh()
    assert _auth(before)['codex_account_grants'][FINGERPRINT][0]['wire_model_id'] == NEW
    assert before.document['codex_seen_history'][0]['pending_wire_model_ids'] == [PENDING]

    assert outbound.deprovision_oauth_provider('codex', owner_user_id=reg.boundary.owner_user_id,
                                              repository=reg.repo)
    account = 'replacement-account'
    reg.token.update(account_id=account, access_token=_bearer(account))
    after = reg.refresh()
    assert _auth(after)['models'] == []
    assert 'codex_account_grants' not in _auth(after)
    assert after.document['codex_seen_history'][0]['account_fingerprint'] == FINGERPRINT
    assert after.document['codex_seen_history'][0]['pending_wire_model_ids'] == []


def test_baseline_requires_fresh_evidence_and_exact_revision(registration):
    reg = registration
    first = reg.refresh()
    reg.token['access_token'] = _bearer('other-account')
    with pytest.raises(ModelRoutingError) as unavailable:
        _opt_in(reg)
    assert unavailable.value.kind == 'codex_baseline_unavailable'
    assert reg.repo.get(reg.boundary).revision == first.revision
    with pytest.raises(ModelRoutingError) as stale:
        establish_owner_codex_baseline(reg.repo, reg.boundary, expected_revision=first.revision - 1)
    assert stale.value.kind == 'model_routing_revision_conflict'
