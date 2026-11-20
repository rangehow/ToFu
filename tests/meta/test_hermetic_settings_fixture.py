"""Network-free specifications for the opt-in Settings interception boundary."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.support.hermetic_settings import (
    FIXTURE_ORIGIN,
    HermeticSettingsRoutes,
    settings_fixture_context,
)

pytestmark = pytest.mark.unit


class StubRoute:
    def __init__(self, path, method='GET', origin=FIXTURE_ORIGIN):
        self.request = SimpleNamespace(url=origin + path, method=method)
        self.responses = []

    async def abort(self, reason):
        self.responses.append(('abort', reason))

    async def fulfill(self, **response):
        self.responses.append(('fulfill', response))

    def continue_(self):
        pytest.fail('No request may leave the fixture')

    def fallback(self):
        pytest.fail('No request may reach another handler')

    def fetch(self):
        pytest.fail('No request may fetch from a server')


@pytest.mark.parametrize(('path', 'expected'), [
    ('/api/v4/browser/status', {'connected': False, 'clients': [], 'capabilities': []}),
    ('/api/v4/browser/adapters', {'adapters': [], 'count': 0, 'available_count': 0}),
    ('/api/v4/browser/access', {'read_denied_domains': [], 'write_grants': []}),
    ('/api/v4/desktop/status', {'download_url': None, 'downloads': []}),
])
def test_ordinary_settings_reads_are_synthetic(path, expected):
    policy = HermeticSettingsRoutes()
    route = StubRoute(path)
    asyncio.run(policy.handle(route))
    assert route.responses == [('fulfill', {
        'json': {'data': expected, 'meta': {'requestId': 'hermetic-settings', 'serverTimeMs': 0}},
    })]
    assert policy.allowed == [('GET', path)]
    assert policy.denied == []
    assert all(method == 'GET' for method, _ in policy.allowed)


@pytest.mark.parametrize('path', [
    '/api/v4/unknown-read', '/api/v4/browser/test', '/api/v4/server-config',
    '/api/v4/browser/status/extra', '/api/v4/browser/status?unknown=1',
    '/api/v4/browser/status#fragment', '/unlisted.js', '/',
])
def test_unknown_non_mutations_fail_closed(path):
    policy = HermeticSettingsRoutes()
    route = StubRoute(path)
    asyncio.run(policy.handle(route))
    assert route.responses == [('abort', 'blockedbyclient')]
    assert policy.allowed == []
    assert len(policy.denied) == 1
    assert policy.denied[0][0] == 'GET'


@pytest.mark.parametrize('origin', ['http://unlisted.invalid', FIXTURE_ORIGIN + ':80',
                                  'https://tofu-settings.invalid', 'http://user@tofu-settings.invalid'])
def test_even_known_reads_cannot_target_other_origins(origin):
    policy = HermeticSettingsRoutes()
    route = StubRoute('/api/v4/browser/status', origin=origin)
    asyncio.run(policy.handle(route))
    assert route.responses == [('abort', 'blockedbyclient')]
    assert not policy.allowed


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'])
def test_method_guard_without_submitting_any_device_command(method):
    policy = HermeticSettingsRoutes()
    route = StubRoute('/api/v4/browser/status', method=method)
    asyncio.run(policy.handle(route))
    assert route.responses == [('abort', 'blockedbyclient')]
    assert policy.denied == [(method, '/api/v4/browser/status')]
    assert not policy.allowed


def test_assets_are_explicit_in_memory_and_cannot_override_api():
    policy = HermeticSettingsRoutes({'/stub.html': ('text/html', '<p>Fixture only</p>')})
    route = StubRoute('/stub.html')
    asyncio.run(policy.handle(route))
    assert route.responses == [('fulfill', {'content_type': 'text/html', 'body': '<p>Fixture only</p>'})]
    with pytest.raises(ValueError, match='exact non-API'):
        HermeticSettingsRoutes({'/api/v4/browser/status': ('text/plain', 'override')})
    other = HermeticSettingsRoutes()
    assert other.assets == {} and other.allowed == []


def test_fresh_context_blocks_workers_and_websockets_before_use_and_closes():
    asyncio.run(_context_lifetime())


async def _context_lifetime():
    calls = []
    async def route(pattern, handler):
        calls.append(('http', pattern, handler))

    async def route_web_socket(pattern, handler):
        calls.append(('websocket', pattern, handler))

    async def close():
        calls.append(('close',))

    context = SimpleNamespace(route=route, route_web_socket=route_web_socket, close=close)

    async def new_context(**options):
        calls.append(('context', options))
        return context

    browser = SimpleNamespace(new_context=new_context)
    with pytest.raises(RuntimeError, match='test consumer failure'):
        async with settings_fixture_context(browser) as (created, policy):
            assert created is context
            assert calls == [('context', {'service_workers': 'block'}),
                             ('http', '**/*', policy.handle),
                             ('websocket', '**/*', policy.block_websocket)]
            closed = []
            async def close_socket(**options):
                closed.append(options)

            websocket = SimpleNamespace(url='ws://tofu-settings.invalid/events', close=close_socket)
            await policy.block_websocket(websocket)
            assert closed == [{'code': 1008, 'reason': 'Fixture denies WebSocket connections'}]
            assert policy.websockets == [websocket.url]
            raise RuntimeError('test consumer failure')
    assert calls[-1] == ('close',)
