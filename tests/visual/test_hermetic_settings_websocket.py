"""Real Chromium fixture safety only; no application modules or server."""
import asyncio

import pytest

from tests.support.hermetic_settings import FIXTURE_ORIGIN, settings_fixture_context

pytestmark = pytest.mark.visual


def test_default_deny_websocket_closes_and_context_retires(monkeypatch):
    from playwright.async_api import WebSocketRoute
    from tests.conftest import _ensure_chromium_library_path

    def no_server(*args, **kwargs):
        pytest.fail('A fixture WebSocket must never connect to a server')

    monkeypatch.setattr(WebSocketRoute, 'connect_to_server', no_server)
    _ensure_chromium_library_path()
    asyncio.run(asyncio.wait_for(_browser_journey(), timeout=30))


async def _browser_journey():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu'], timeout=15000)
        try:
            for exceptional_exit in (False, True):
                await _context_journey(browser, exceptional_exit)
            assert browser.contexts == []
        finally:
            await browser.close()


async def _context_journey(browser, exceptional_exit):
    page = None
    closed = []
    errors = []
    try:
        async with settings_fixture_context(browser, assets={
            '/inert.html': ('text/html', '<!doctype html><title>Inert fixture</title><p>No app boot</p>'),
        }) as (context, policy):
            context.on('close', lambda _: closed.append(True))
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(FIXTURE_ORIGIN + '/inert.html')
            result = await page.evaluate('''async () => {
              const result = await Promise.all(['ws://tofu-settings.invalid/denied',
                'wss://unlisted.invalid/denied'].map(url => new Promise(resolve => {
                  const ws = new WebSocket(url);
                  ws.onclose = event => resolve({code:event.code, reason:event.reason, state:ws.readyState});
              })));
              const blocked = await fetch('/unknown').then(() => false, () => true);
              return {result, blocked};
            }''')
            assert result == {'result': [
                {'code': 1008, 'reason': 'Fixture denies WebSocket connections', 'state': 3},
                {'code': 1008, 'reason': 'Fixture denies WebSocket connections', 'state': 3},
            ], 'blocked': True}
            assert sorted(policy.websockets) == ['ws://tofu-settings.invalid/denied', 'wss://unlisted.invalid/denied']
            assert policy.allowed == [('GET', '/inert.html')]
            assert policy.denied == [('GET', '/unknown')]
            assert errors == []
            if exceptional_exit:
                raise RuntimeError('consumer failure')
    except RuntimeError as error:
        assert exceptional_exit and str(error) == 'consumer failure'
    assert page.is_closed()
    assert closed == [True]
    assert browser.contexts == []
