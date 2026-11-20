"""Black-box HTTP/WebSocket behaviour any Tofu server must keep.

Speaks only HTTP and a raw WebSocket handshake over ``blackbox_url`` with the
standard library, and reads only committed contracts:
``contracts/api_endpoint_index_v1.json`` (routes, ``public``, ``requiredScopes``),
``contracts/api_v4.yaml`` (problem schema, legacy-upgrade policy, meta) and the
rules in ``docs/contracts/API_CONTRACT.md``. Every request is side-effect free:
reads, and writes the authentication boundary refuses before any handler runs.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

pytestmark = pytest.mark.api

ROOT = Path(__file__).resolve().parents[2]
ENDPOINT_INDEX = json.loads((ROOT / 'contracts/api_endpoint_index_v1.json').read_text('utf-8'))
API_V4 = yaml.safe_load((ROOT / 'contracts/api_v4.yaml').read_text('utf-8'))
PROBLEM_SCHEMA = API_V4['components']['schemas']['Problem']
PROBLEM_MEDIA_TYPE = 'application/problem+json'
CROSS_SITE = {'Origin': 'https://cross-site.blackbox.invalid', 'Sec-Fetch-Site': 'cross-site'}
# API_CONTRACT.md §0: the browser's crash/report endpoints aside, a v4 write
# from another site's page never borrows the open-mode local grant. This route
# only prices a usage document (no durable effect) and declares a scope.
SCOPED_WRITE = ('POST', '/api/v4/messages/cost')


class Reply:
    def __init__(self, status: int, headers, body: bytes):
        self.status = status
        self.headers = {key.lower(): value for key, value in headers.items()}
        self.body = body

    def json(self):
        return json.loads(self.body)


def request(base: str, path: str, *, method: str = 'GET', headers=None, body: bytes | None = None) -> Reply:
    outgoing = urllib.request.Request(base + path, method=method, headers=headers or {}, data=body)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(outgoing, timeout=60) as response:
            return Reply(response.status, response.headers, response.read())
    except urllib.error.HTTPError as error:
        return Reply(error.code, error.headers, error.read())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def websocket_handshake(base: str, path: str, headers=None) -> int:
    """Send one RFC 6455 upgrade request and return the HTTP status line's code."""
    target = urlsplit(base)
    secure = target.scheme == 'https'
    port = target.port or (443 if secure else 80)
    raw = socket.create_connection((target.hostname, port), timeout=60)
    connection = ssl.create_default_context().wrap_socket(raw, server_hostname=target.hostname) if secure else raw
    with connection:
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        lines = [f'GET {path} HTTP/1.1', f'Host: {target.netloc}', 'Upgrade: websocket',
                 'Connection: Upgrade', f'Sec-WebSocket-Key: {key}', 'Sec-WebSocket-Version: 13',
                 *(f'{name}: {value}' for name, value in (headers or {}).items())]
        connection.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('ascii'))
        received = b''
        while b'\r\n' not in received:
            chunk = connection.recv(4096)
            if not chunk:
                break
            received += chunk
    status_line = received.split(b'\r\n', 1)[0].decode('latin-1')
    match = re.match(r'HTTP/1\.[01] (\d{3})', status_line)
    assert match, f'no HTTP status line in upgrade reply: {status_line!r}'
    return int(match.group(1))


def assert_problem(reply: Reply, status: int, code: str | None = None) -> dict:
    assert reply.status == status
    assert reply.headers['content-type'].split(';')[0] == PROBLEM_MEDIA_TYPE
    assert reply.headers.get('cache-control') == 'no-store'
    problem = reply.json()
    assert set(PROBLEM_SCHEMA['required']) <= set(problem)
    assert set(problem) <= set(PROBLEM_SCHEMA['properties'])
    assert problem['status'] == status
    assert re.fullmatch(PROBLEM_SCHEMA['properties']['code']['pattern'], problem['code'])
    if code is not None:
        assert problem['code'] == code
    return problem


def endpoint(method: str, path: str) -> dict:
    matches = [e for e in ENDPOINT_INDEX['endpoints'] if e['method'] == method and e['path'] == path]
    assert len(matches) == 1, f'{method} {path} is not declared once in the endpoint index'
    return matches[0]


def public_static_get_paths() -> list[str]:
    return sorted(e['path'] for e in ENDPOINT_INDEX['endpoints']
                  if e['method'] == 'GET' and e['transport'] == 'http' and e['public'] is True
                  and '<' not in e['path'])


def auth_mode(base: str) -> str:
    reply = request(base, '/api/v4/auth/mode')
    assert reply.status == 200
    return reply.json()['data']['mode']


def credential_free_access(base: str) -> bool:
    """Whether this harness gets the open-mode local grant without a credential.

    Open mode grants loopback peers only (API_CONTRACT.md, identity); a
    non-loopback target's answer depends on TOFU_OPEN_MODE_ALLOW_REMOTE, which
    is not observable over HTTP, so such a case is skipped.
    """
    if auth_mode(base) != 'open':
        return False
    host = urlsplit(base).hostname or ''
    if host == 'localhost' or host.startswith('127.') or host == '::1':
        return True
    pytest.skip('open mode over a non-loopback address depends on TOFU_OPEN_MODE_ALLOW_REMOTE')


# ── Index and static assets ──────────────────────────────────────────


def test_index_is_an_uncached_html_shell_or_the_sign_in_refusal(blackbox_url):
    reply = request(blackbox_url, '/')
    assert reply.headers['content-type'].startswith('text/html')
    if credential_free_access(blackbox_url):
        assert reply.status == 200
        assert reply.headers['cache-control'] == 'no-store'
        assert '/static/vite/assets/' in reply.body.decode('utf-8', 'replace')
    else:
        # Default deny: the application shell needs a credential.
        assert reply.status == 401


def test_hashed_vite_assets_are_immutable_and_the_manifest_is_not_cached(blackbox_url):
    manifest = request(blackbox_url, '/static/vite/manifest.json')
    assert manifest.status == 200
    assert manifest.headers['cache-control'] == 'no-store'
    entries = sorted({chunk['file'] for chunk in manifest.json().values()
                      if chunk.get('isEntry') and chunk['file'].endswith('.js')})
    assert entries, 'the Vite manifest names no entry script'
    for entry in entries:
        reply = request(blackbox_url, f'/static/vite/{entry}')
        assert reply.status == 200, entry
        assert reply.headers['content-type'].startswith('text/javascript'), entry
        assert reply.headers['cache-control'] == 'public, max-age=31536000, immutable', entry


# ── Public endpoints declared by the index ───────────────────────────


def test_index_declares_public_reads():
    assert {'/api/v4/capabilities', '/api/v4/auth/mode'} <= set(public_static_get_paths())


@pytest.mark.parametrize('path', public_static_get_paths())
def test_every_public_get_route_answers_without_a_server_error(blackbox_url, path):
    reply = request(blackbox_url, path)
    assert reply.status < 500, (path, reply.status, reply.body[:200])


def test_capabilities_answer_the_v4_success_envelope(blackbox_url):
    reply = request(blackbox_url, '/api/v4/capabilities')
    assert reply.status == 200
    assert reply.headers['content-type'].split(';')[0] == 'application/json'
    document = reply.json()
    assert set(document) == {'data', 'meta'}
    assert isinstance(document['data'], dict)
    assert document['meta']['requestId']


def test_meta_names_the_contract_major_and_minimum_builds(blackbox_url):
    reply = request(blackbox_url, '/api/v4/meta')
    assert reply.status == 200
    data = reply.json()['data']
    assert data['apiMajor'] == API_V4['x-tofu-api-major']
    minimum = API_V4['x-tofu-minimum-builds']
    assert data['minDesktopBuild'] == minimum['desktop']
    assert data['minAndroidBuild'] == minimum['android']


# ── Error envelopes ──────────────────────────────────────────────────


def test_unknown_v4_path_answers_the_v4_problem(blackbox_url):
    path = '/api/v4/blackbox-harness/no-such-resource'
    if credential_free_access(blackbox_url):
        problem = assert_problem(request(blackbox_url, path), 404, 'not_found')
    else:
        # Default deny decides before routing, so an unknown path reveals nothing.
        problem = assert_problem(request(blackbox_url, path), 401, 'permission')
    assert problem['instance'] == path


@pytest.mark.parametrize('prefix', API_V4['x-tofu-legacy-upgrade']['prefixes'])
def test_retired_api_major_answers_upgrade_required(blackbox_url, prefix):
    policy = API_V4['x-tofu-legacy-upgrade']
    assert policy['activeByDefault'] is True
    path = f'{prefix}/conversations'
    problem = assert_problem(request(blackbox_url, path), policy['status'], 'api_version_upgrade_required')
    assert problem['upgradeUrl'] == policy['upgradeUrl']
    assert problem['instance'] == path


def test_retired_api_major_is_refused_before_authentication_or_body_parsing(blackbox_url):
    policy = API_V4['x-tofu-legacy-upgrade']
    reply = request(blackbox_url, f'{policy["prefixes"][0]}/conversations', method='POST',
                    headers={**CROSS_SITE, 'Content-Type': 'application/json'}, body=b'{not json')
    assert_problem(reply, policy['status'], 'api_version_upgrade_required')


def test_unauthenticated_cross_site_write_to_a_scoped_route_is_refused(blackbox_url):
    method, path = SCOPED_WRITE
    assert endpoint(method, path)['requiredScopes'], f'{path} must declare a scope'
    reply = request(blackbox_url, path, method=method,
                    headers={**CROSS_SITE, 'Content-Type': 'application/json'}, body=b'{}')
    problem = assert_problem(reply, 401)
    assert problem['instance'] == path


# ── Push WebSocket ───────────────────────────────────────────────────


def test_index_declares_the_push_websocket():
    assert endpoint('GET', '/api/push')['transport'] == 'websocket'


def test_cross_site_push_websocket_upgrade_is_refused(blackbox_url):
    # Browsers apply no CORS to sockets; another site's page never gets the
    # owner's push stream, in any auth mode.
    assert websocket_handshake(blackbox_url, '/api/push', CROSS_SITE) == 401


def test_push_websocket_upgrade_follows_the_auth_mode(blackbox_url):
    expected = 101 if credential_free_access(blackbox_url) else 401
    # Open mode keeps the zero-setup loopback client; every other mode refuses
    # the upgrade without a credential.
    assert websocket_handshake(blackbox_url, '/api/push') == expected
