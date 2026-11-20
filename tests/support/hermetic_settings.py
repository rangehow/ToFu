"""Opt-in Settings read prerequisite: in-memory HTTP only, never a live app."""
from contextlib import asynccontextmanager
from copy import deepcopy
from urllib.parse import urlsplit

import pytest

FIXTURE_ORIGIN = 'http://tofu-settings.invalid'


class HermeticSettingsRoutes:
    def __init__(self, assets=None):
        self.assets = dict(assets or {})
        if any(not path.startswith('/') or path.startswith('//') or '?' in path
               or '#' in path or path.startswith('/api/') for path in self.assets):
            raise ValueError('Assets must be exact non-API paths')
        self.reads = {
            '/api/v4/browser/status': {'connected': False, 'clients': [], 'capabilities': []},
            '/api/v4/browser/adapters': {'adapters': [], 'count': 0, 'available_count': 0},
            '/api/v4/browser/access': {'read_denied_domains': [], 'write_grants': []},
            '/api/v4/desktop/status': {'download_url': None, 'downloads': []},
        }
        self.allowed = []
        self.denied = []
        self.websockets = []

    async def handle(self, route):
        request = route.request
        parts = urlsplit(request.url)
        origin = f'{parts.scheme}://{parts.netloc}'
        key = (request.method, parts.path)
        if origin != FIXTURE_ORIGIN or parts.query or parts.fragment or request.method != 'GET':
            self.denied.append(key)
            await route.abort('blockedbyclient')
        elif parts.path in self.reads:
            self.allowed.append(key)
            await route.fulfill(json={'data': deepcopy(self.reads[parts.path]),
                                'meta': {'requestId': 'hermetic-settings', 'serverTimeMs': 0}})
        elif parts.path in self.assets:
            self.allowed.append(key)
            content_type, body = self.assets[parts.path]
            await route.fulfill(content_type=content_type, body=body)
        else:
            self.denied.append(key)
            await route.abort('blockedbyclient')

    async def block_websocket(self, websocket):
        self.websockets.append(websocket.url)
        await websocket.close(code=1008, reason='Fixture denies WebSocket connections')


@asynccontextmanager
async def settings_fixture_context(browser, *, assets=None):
    """Own a fresh context; register interception before a page can exist."""
    routes = HermeticSettingsRoutes(assets)
    context = await browser.new_context(service_workers='block')
    try:
        await context.route('**/*', routes.handle)
        await context.route_web_socket('**/*', routes.block_websocket)
        yield context, routes
    finally:
        await context.close()


@pytest.fixture
def hermetic_settings_context():
    """Return the async context factory; callers own an async Playwright browser."""
    return settings_fixture_context
