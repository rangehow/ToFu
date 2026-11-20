"""Revocation at iLink's prepared-request wire boundary, with no network IO."""
import json

import pytest
import requests

from lib.channels.bots.platform import failure
from lib.channels.wechat import protocol
from lib.channels.wechat.transport import WeChatTransport
from lib.http_transport import client, outbound
from tests.channels.test_wechat_files import ORIGINAL, _response

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('target,cut', [
    ('caption', 'token'), ('caption', 'headers'), ('caption', 'prepared'),
    ('setup', 'token'), ('setup', 'headers'), ('setup', 'prepared'),
    ('cdn', 'previous_response'), ('cdn', 'prepared'),
    ('file', 'previous_response'), ('file', 'prepared'),
])
def test_revocation_before_physical_admission(tmp_path, monkeypatch, target, cut):
    path = tmp_path / 'data.pdf'
    path.write_bytes(ORIGINAL)
    revoked = False
    calls = []

    def refuse():
        if revoked:
            return failure('unsupported_target', 'fixture revoked')

    def token():
        nonlocal revoked
        revoked |= cut == 'token'
        return 'fake-token'

    orig_headers = protocol.bot_headers

    def headers(value):
        nonlocal revoked
        result = orig_headers(value)
        revoked |= cut == 'headers'
        return result

    monkeypatch.setattr(protocol, 'bot_headers', headers)

    def physical(request, **kwargs):
        nonlocal revoked
        assert kwargs['allow_redirects'] is False or request.method == 'POST'
        if '/getuploadurl' in request.url:
            kind = 'setup'
            response = _response(200, data=b'{"upload_param":"fixture"}')
        elif '/upload?' in request.url:
            kind = 'cdn'
            assert 'Authorization' not in request.headers
            response = _response(200, headers={'x-encrypted-param': 'fixture'})
        else:
            kind = 'caption' if json.loads(request.body)['msg']['item_list'][0]['type'] == 1 else 'file'
            response = _response(200, data=b'{"ret":0}')
        calls.append(kind)
        assert not revoked
        if cut == 'previous_response':
            revoked |= (kind, target) in {('setup', 'cdn'), ('cdn', 'file')}
        return response

    def prepared(method, url, *, headers, data, **options):
        nonlocal revoked
        request = requests.Request(method, url, headers=headers, data=data).prepare()
        kind = ('setup' if '/getuploadurl' in url else 'cdn' if '/upload?' in url else
                'caption' if target == 'caption' else 'file')
        revoked |= cut == 'prepared' and target == kind
        return outbound.send_routed_request(physical, request,
            policy=outbound.RequestNetworkPolicy(explicit_proxy=True), **options)

    monkeypatch.setattr(client, '_sync_request', prepared)
    monkeypatch.setattr('lib.proxy.report_network_route', lambda *a, **kw: None)
    transport = WeChatTransport(token)
    reply = {'contextToken': 'fixture'}
    if target == 'caption':
        result = transport.send('uid', 'fixture', 'caption', item_id='id', reply=reply, before_submit=refuse)
    else:
        result = transport.send_attachment('uid', 'fixture', str(path), item_id='id', reply=reply,
                                           before_submit=refuse, before_transfer=refuse)
    assert result['error']['code'] == 'unsupported_target'
    assert calls == {'caption': [], 'setup': [], 'cdn': ['setup'], 'file': ['setup', 'cdn']}[target]


def test_wrapped_http_uses_only_actual_admission_and_checks_every_route(monkeypatch):
    from types import SimpleNamespace
    from lib.channels.bots.http import request_json

    revoked = False
    checks, attempts = [], []

    def authority():
        checks.append(revoked)
        return failure('unsupported_target', 'fixture revoked') if revoked else None

    def physical(request, **options):
        nonlocal revoked
        attempts.append(request.url)
        revoked = True
        raise requests.ConnectTimeout('fixture failed before any application bytes')

    routes = [SimpleNamespace(route_id=i, requests_proxies=lambda: {}) for i in range(2)]
    monkeypatch.setattr('lib.proxy.network_route_candidates', lambda *a, **kw: routes)
    monkeypatch.setattr('lib.proxy.report_network_route', lambda *a, **kw: None)
    monkeypatch.setattr('lib.network_routes.is_safe_connect_failure', lambda exc: True)

    def prepared(method, url, *, headers, data, **options):
        assert checks == []  # a managed wrapper must not mark at logical request entry
        request = requests.Request(method, url, headers=headers, data=data).prepare()
        return outbound.send_routed_request(physical, request,
            policy=outbound.RequestNetworkPolicy(explicit_proxy=False), **options)

    monkeypatch.setattr(client, '_sync_request', prepared)
    transport = WeChatTransport(lambda: 'fixture', http=lambda *a, **kw: request_json(*a, **kw))
    result = transport.send('uid', 'fixture', 'caption', item_id='id', reply={'contextToken': 'fixture'},
                            before_submit=authority)
    assert result['error']['code'] == 'unsupported_target'
    assert checks == [False, True] and len(attempts) == 1
