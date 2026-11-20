"""A template probe can only attest the transport it actually exercised."""
from types import SimpleNamespace

import requests

import pytest
from requests.structures import CaseInsensitiveDict

from lib.http_transport import client
from lib.model_routing import template_supply

pytestmark = pytest.mark.unit

URL = 'https://synthetic.invalid/v1'
WIRE = 'synthetic-wire'
ROOT = 'synthetic-model'
SELECTOR = 'x-context-provider'


@pytest.mark.parametrize(
    'face, envelope, target_selector, sent_selector, deployment_headers, status', [
    pytest.param('tokrouter', {SELECTOR: 'kunlun'}, 'tokrouter', 'kunlun',
                 {}, 'unprobed', id='credential-overrides-declared-face'),
    pytest.param('tokrouter', {SELECTOR.upper(): 'kunlun'}, 'tokrouter', 'kunlun',
                 {}, 'unprobed', id='credential-overrides-case-insensitively'),
    pytest.param('tokrouter', {SELECTOR: 'kunlun'}, 'kunlun', 'kunlun',
                 {}, 'passed', id='evidence-identifies-the-face-actually-sent'),
    pytest.param('', {SELECTOR: 'kunlun'}, '', 'kunlun', {}, 'unprobed',
                 id='credential-adds-a-declared-selector-to-default'),
    pytest.param('', {}, '', None, {}, 'passed', id='ordinary-bare-deployment'),
    pytest.param('', {'X-Account-Lane': 'synthetic-route'}, '', None,
                 {}, 'unprobed', id='unknown-credential-selector-vs-bare-deployment'),
    pytest.param('', {'X-Account-Lane': 'synthetic-route'}, '', None,
                 {'x-account-lane': 'synthetic-route'}, 'passed',
                 id='unknown-selector-matches-deployment-case-insensitively'),
    pytest.param('', {'X-Account-Lane': 'synthetic-route'}, '', None,
                 {'x-account-lane': 'other-route'}, 'unprobed',
                 id='unknown-selector-differs-from-deployment'),
    pytest.param('', {'X-Api-Key-Route': 'synthetic-route'}, '', None,
                 {}, 'unprobed', id='auth-looking-name-is-not-an-auth-contract'),
    pytest.param('tokrouter', {SELECTOR.upper(): 'tokrouter'}, 'tokrouter',
                 'tokrouter', {}, 'passed', id='same-selector-value-with-other-casing'),
    pytest.param('legacy', {SELECTOR: 'kunlun'}, 'kunlun', 'kunlun', {}, 'passed',
                 id='credential-wins-over-both-face-spellings'),
])
def test_persisted_probe_evidence_matches_the_transmitted_face(
        monkeypatch, face, envelope, target_selector, sent_selector, deployment_headers, status):
    sent = []

    def post(url, *, json: dict, headers, timeout):
        request = requests.Request('POST', url, json=json, headers=headers).prepare()
        sent.append((request.url, json['model'], CaseInsensitiveDict(
            (name, request.headers[name]) for name in headers)))
        return SimpleNamespace(request=request,
            status_code=200, text='{"output_text":"hello"}')

    monkeypatch.setattr(client, 'http_post', post)
    faces = {name: {'base_url': URL, 'protocol': 'responses',
                    'extra_headers': {SELECTOR: name}}
             for name in ('tokrouter', 'kunlun')}
    faces['legacy'] = {**faces['tokrouter'],
                       'extra_headers': {SELECTOR: 'tokrouter', SELECTOR.upper(): 'old'}}
    recipe = {'model_id': ROOT, 'request_ids': [WIRE],
              'request_faces': {WIRE: face} if face else {}}
    verdicts = template_supply._live_prober(
        base_url=URL, protocol='responses', faces=faces,
        api_key='synthetic-key', extra_headers=envelope, recipes=[recipe])
    assert verdicts[ROOT] == 'ok'
    assert 'synthetic-key' not in repr(verdicts)
    assert all(value not in repr(verdicts) for value in envelope.values())
    expected_headers = {'Authorization': 'Bearer synthetic-key', **envelope}
    if sent_selector is not None:
        expected_headers[SELECTOR] = sent_selector
    assert sent == [(URL + '/responses', WIRE, CaseInsensitiveDict(expected_headers))]

    target = {'connection_id': 'connection', 'provider_access_id': 'access',
              'gateway_namespace': target_selector, 'base_url': URL,
              'protocol': 'openai_responses', 'enabled': True,
              'extra_headers': {SELECTOR: target_selector} if target_selector else {}}
    working = {'connections': [target], 'offerings': [], 'deployments': []}
    bundle = {'connections': [target], 'models': [], 'creators': [],
              'offerings': [{'offering_id': 'new', 'pending_model_id': ROOT}],
              'deployments': [{'offering_id': 'new', 'connection_id': 'connection',
                               'wire_model_id': WIRE,
                               'extra_headers': deployment_headers}]}
    stats = {'added_offerings': 0, 'added_deployments': 0, 'accesses': {}}
    assert template_supply._transplant(
        working, {'provider_access_id': 'access'}, bundle, verdicts, stats, [])
    assert working['deployments'][0]['probe_status'] == status


@pytest.mark.parametrize('identity', ['direct', 'redirected', 'missing'])
def test_gateway_overwrite_credits_only_the_prepared_header(monkeypatch, identity):
    from lib.llm import gateway_shape

    monkeypatch.setattr(gateway_shape, 'gateway_anthropic_headers',
                        lambda _: {'X-Gateway-Cwd': '/synthetic/generated'})
    sent = []

    def send(request, **kwargs):
        sent.append(request)
        response = requests.Response()
        if identity != 'missing':
            response.request = request
        response.status_code = 200
        response._content = b'{"content":[{"type":"text","text":"hello"}]}'
        return response

    with requests.Session() as session:
        if identity == 'redirected':
            original_request = session.request

            def redirected(*args, **kwargs):
                response = original_request(*args, **kwargs)
                response.history = [requests.Response()]
                return response

            monkeypatch.setattr(session, 'request', redirected)
        monkeypatch.setattr(session.get_adapter(URL), 'send', send)
        monkeypatch.setattr(client, '_sync_session', lambda: session)
        verdicts = template_supply._live_prober(
            base_url=URL, protocol='anthropic', faces={}, api_key='synthetic-key',
            extra_headers={'X-Gateway-Cwd': '/synthetic/explicit'},
            recipes=[{'model_id': ROOT, 'request_ids': [WIRE]}])
    assert sent[0].headers['X-Gateway-Cwd'] == '/synthetic/generated'
    assert verdicts[ROOT] == 'ok'
    assert verdicts.get((ROOT, *template_supply._probe_transport_key(
        WIRE, URL, 'anthropic', {'X-Gateway-Cwd': '/synthetic/explicit'}))) != 'ok'
    generated = (ROOT, *template_supply._probe_transport_key(
        WIRE, URL, 'anthropic', {'X-Gateway-Cwd': '/synthetic/generated'}))
    assert verdicts.get(generated) == ('ok' if identity == 'direct' else None)


@pytest.mark.parametrize('modality', ['chat', 'nonchat'])
def test_invalid_envelope_header_never_reaches_probe_logs(monkeypatch, caplog, modality):
    from lib.provider.provider_probe import _post_and_classify, probe_one_cell

    marker = 'synthetic-credential-envelope-marker'
    with requests.Session() as session:
        monkeypatch.setattr(client, '_sync_session', lambda: session)
        monkeypatch.setattr(session.get_adapter(URL), 'send',
                            lambda *a, **kw: pytest.fail('invalid header reached send'))
        headers = {'X-Envelope': ' ' + marker}
        if modality == 'chat':
            verdict, detail = probe_one_cell(URL, 'synthetic-key', WIRE, headers, 1)
        else:
            verdict, detail = _post_and_classify(URL, headers, 1, json_body={})
    assert verdict == 'unavailable'
    assert 'InvalidHeader' in detail
    assert 'InvalidHeader' in caplog.text
    assert marker not in detail
    assert marker not in caplog.text
    assert all(marker not in repr(record.args) for record in caplog.records)
