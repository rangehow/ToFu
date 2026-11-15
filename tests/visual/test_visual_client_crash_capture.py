"""Real Chromium: a renderer crash is reported once, after a reload or from another tab.

Drives an isolated in-process server (``live_server``) with its own headless
Chromium, launched like the shared ``browser`` fixture but through the async
API: the renderer never answers ``Page.crash``, which would block a sync call
forever. CDP ``Page.crash`` kills the renderer of a booted Tofu tab; the same
tab is reloaded through CDP, so its sessionStorage survives exactly as it does
behind Chrome's sad-tab Reload button. The server must then hold exactly one
``tofu.client-crash/v1`` record for the crashed page instance, carrying the
breadcrumbs written before the crash, and a later clean reload adds nothing.
A crashed tab that is closed instead is reported once by the replacement tabs
of the same browser session, which race for it; a cleanly closed tab is not.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from tests._jsdom import require_shipped_graph

pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]

_BOOT_FLAG = ("window.addEventListener('tofu:boot-ready', () => {"
              " window.__crashE2eBootReady = (window.__crashE2eBootReady || 0) + 1; });")
# An address without a route lands in the office world unless the owner chose the classic
# view (frontend/src/application-shell/landing.ts); these tabs are the owner's chat tabs.
_CLASSIC_LANDING = "localStorage.setItem('tofu_landing_workspace', 'chat');"


def _records(store) -> list[dict]:
    path = store.current_path()
    if not path.is_file():
        return []
    return [record for record in map(json.loads, path.read_text(encoding='utf-8').splitlines())
            if record.get('schema') == 'tofu.client-crash/v1']


async def _evaluate(cdp, expression: str):
    result = await cdp.send('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
    return result.get('result', {}).get('value')


async def _until(predicate, *, seconds: float, what: str):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.25)
    raise AssertionError(f'timed out waiting for {what}')


async def _launch_browser(playwright):
    launch = {'headless': True, 'args': ['--no-sandbox', '--disable-gpu']}
    executable = os.environ.get('TOFU_E2E_EXECUTABLE_PATH', '').strip()
    if executable:
        launch['executable_path'] = executable
    try:
        return await playwright.chromium.launch(**launch)
    except Exception as error:
        pytest.skip(f'chromium build unavailable / failed to launch: {error}')


async def _crash_reload_and_settle(base_url: str, store) -> dict:
    from playwright.async_api import async_playwright
    from chromium_env import ensure_chromium_env

    ensure_chromium_env()
    async with async_playwright() as playwright:
        browser = await _launch_browser(playwright)
        try:
            context = await browser.new_context(service_workers='block')
            await context.add_init_script(_BOOT_FLAG)
            await context.add_init_script(_CLASSIC_LANDING)
            page = await context.new_page()
            await page.goto(base_url + '/', wait_until='domcontentloaded')
            await page.wait_for_function('window.__crashE2eBootReady >= 1', timeout=60000)
            crashed_page = await page.evaluate(
                "JSON.parse(sessionStorage.getItem('tofu.crash.v1.alive')).id")
            await page.evaluate("""() => {
                console.error('[crash-e2e] console error before the crash');
                setTimeout(() => { throw new Error('crash-e2e uncaught'); });
                Promise.reject(new Error('crash-e2e rejection'));
            }""")
            await page.wait_for_function("""() => Object.keys(sessionStorage)
                .filter(key => key.startsWith('tofu.crash.v1.') && key.includes('.e.')).length >= 3""")
            # The journal initialized Chrome's crash report buffer (set() throws before that).
            crash_buffer = await page.evaluate("""() => {
                if (!window.crashReport) return 'unsupported';
                try { window.crashReport.set('e2eProbe', '1'); return 'initialized'; }
                catch (error) { return error.name; }
            }""")
            crashed = asyncio.Event()
            page.on('crash', lambda _page: crashed.set())
            cdp = await context.new_cdp_session(page)
            # The renderer never answers Page.crash; the crash event is the outcome.
            sender = asyncio.create_task(cdp.send('Page.crash'))
            await asyncio.wait_for(crashed.wait(), 30)
            sender.cancel()
            # Same tab, same sessionStorage: what the sad tab's Reload button does.
            await cdp.send('Page.reload')
            await _until(lambda: asyncio.sleep(0, [r for r in _records(store)
                                                    if r['pageInstanceId'] == crashed_page]),
                         seconds=90, what='the crash record of the reloaded tab')
            reloaded_page = await _evaluate(cdp, "JSON.parse(sessionStorage.getItem('tofu.crash.v1.alive')).id")
            # A clean reload afterwards must not report anything again.
            await cdp.send('Page.reload')
            await _until(lambda: _evaluate(cdp, """(() => {
                const alive = JSON.parse(sessionStorage.getItem('tofu.crash.v1.alive') || 'null');
                return window.__crashE2eBootReady >= 1 && alive && alive.id !== %s;
            })()""" % json.dumps(reloaded_page)), seconds=60, what='the clean reload to boot')
            pending = await _evaluate(cdp, """Object.keys(sessionStorage)
                .filter(key => key.endsWith('.verdict'))""")
            return {'crashed': crashed_page, 'reloaded': reloaded_page, 'pending': pending,
                    'crash_buffer': crash_buffer}
        finally:
            await browser.close()


_JOURNAL_STATE = """async (ids) => ({
    keys: Object.keys(localStorage).filter(key => ids.some(id => key.includes(id))),
    held: (await navigator.locks.query()).held.map(lock => lock.name)
        .filter(name => ids.some(id => name.endsWith(id))),
})"""


async def _settled(page, ids):
    state = await page.evaluate(_JOURNAL_STATE, ids)
    return state if not state['keys'] else None


async def _crash_close_and_replace(base_url: str, store) -> dict:
    from playwright.async_api import async_playwright
    from chromium_env import ensure_chromium_env

    ensure_chromium_env()
    async with async_playwright() as playwright:
        browser = await _launch_browser(playwright)
        try:
            context = await browser.new_context(service_workers='block')
            await context.add_init_script(_BOOT_FLAG)
            await context.add_init_script(_CLASSIC_LANDING)

            async def boot():
                page = await context.new_page()
                await page.goto(base_url + '/', wait_until='domcontentloaded')
                await page.wait_for_function('window.__crashE2eBootReady >= 1', timeout=60000)
                return page

            async def published(page) -> str:
                instance = await page.evaluate("JSON.parse(sessionStorage.getItem('tofu.crash.v1.alive')).id")
                # A booted, visible page of a known owner publishes its origin record.
                await page.wait_for_function("(id) => localStorage.getItem(`tofu.crash.v1.${id}.live`) !== null",
                                             arg=instance, timeout=60000)
                return instance

            clean = await boot()
            clean_id = await published(clean)
            await clean.close()
            doomed = await boot()
            doomed_id = await published(doomed)
            crashed = asyncio.Event()
            doomed.on('crash', lambda _page: crashed.set())
            cdp = await context.new_cdp_session(doomed)
            # The renderer never answers Page.crash; the crash event is the outcome.
            sender = asyncio.create_task(cdp.send('Page.crash'))
            await asyncio.wait_for(crashed.wait(), 30)
            sender.cancel()
            # The owner closes the sad tab instead of reloading it.
            await doomed.close()
            replacements = await asyncio.gather(boot(), boot())
            await _until(lambda: asyncio.sleep(0, [r for r in _records(store) if r['pageInstanceId'] == doomed_id]),
                         seconds=90, what='the crash record from a replacement tab')
            # The reporter deletes the record once the server took it, and keeps its claim.
            state = await _until(lambda: _settled(replacements[0], [doomed_id, clean_id]), seconds=60,
                                 what='the delivered record to leave localStorage')
            return {'doomed': doomed_id, 'clean': clean_id, 'state': state}
        finally:
            await browser.close()


def test_renderer_crash_is_recorded_once_with_breadcrumbs(live_server, tmp_path, monkeypatch):
    require_shipped_graph()
    pytest.importorskip('playwright.async_api')
    if (os.environ.get('TOFU_E2E_BROWSER') or 'chromium').lower() != 'chromium':
        pytest.skip('CDP Page.crash needs Chromium')
    monkeypatch.setenv('TOFU_CLIENT_REPORTS_DIR', str(tmp_path))
    import lib.router.watchdog.signals as signals
    from lib import client_report_store as store
    calls: list[dict] = []
    monkeypatch.setattr(signals, 'report_client_crash',
                        lambda owner, **kwargs: calls.append({'owner': owner, **kwargs}))

    outcome = asyncio.run(_crash_reload_and_settle(live_server, store))

    records = [record for record in _records(store) if record['channel'] == 'unclean_exit']
    assert [record['pageInstanceId'] for record in records] == [outcome['crashed']]
    [record] = records
    assert record['classification'] == 'renderer_crash_suspected'
    assert record['navigationType'] == 'reload'
    assert record['lastSeenAt'] >= record['startedAt'] and record['detectedAt'] >= record['lastSeenAt']
    messages = [(error['kind'], error['message']) for error in record['errors']]
    assert ('console', '[crash-e2e] console error before the crash') in messages
    assert any(kind == 'error' and 'crash-e2e uncaught' in message for kind, message in messages)
    assert ('unhandledrejection', 'crash-e2e rejection') in messages
    assert record['context']['view'] == 'chat' and record['context']['streamActive'] is False
    assert 'Chrome' in record['environment']['userAgent']
    assert outcome['pending'] == [], 'a delivered episode must leave no pending verdict'
    assert outcome['crash_buffer'] == 'initialized', outcome['crash_buffer']
    crash_calls = [call for call in calls if call['page_instance_id'] == outcome['crashed']]
    assert crash_calls and {call['kind'] for call in crash_calls} <= {
        'browser_renderer_crash', 'browser_renderer_oom', 'browser_renderer_unresponsive'}
    # Chrome's own crash report (Reporting API) may also arrive for the same page;
    # it never adds a second unclean-exit record, and it names the same page.
    browser_reports = [record for record in _records(store) if record['channel'] == 'reporting_api']
    assert all(item['pageInstanceId'] in (None, outcome['crashed']) for item in browser_reports)


def test_crashed_tab_closed_and_replaced_is_recorded_once_from_another_tab(live_server, tmp_path, monkeypatch):
    require_shipped_graph()
    pytest.importorskip('playwright.async_api')
    if (os.environ.get('TOFU_E2E_BROWSER') or 'chromium').lower() != 'chromium':
        pytest.skip('CDP Page.crash needs Chromium')
    monkeypatch.setenv('TOFU_CLIENT_REPORTS_DIR', str(tmp_path))
    import lib.router.watchdog.signals as signals
    from lib import client_report_store as store
    calls: list[dict] = []
    monkeypatch.setattr(signals, 'report_client_crash',
                        lambda owner, **kwargs: calls.append({'owner': owner, **kwargs}), raising=False)

    outcome = asyncio.run(_crash_close_and_replace(live_server, store))

    records = [record for record in _records(store) if record['channel'] == 'unclean_exit']
    assert [record['pageInstanceId'] for record in records] == [outcome['doomed']]
    [record] = records
    assert record['classification'] == 'renderer_crash_suspected'
    assert record['navigationType'] == 'navigate'
    assert record['startedAt'] <= record['lastSeenAt'] <= record['detectedAt']
    assert record['context']['view'] == 'chat' and 'Chrome' in record['environment']['userAgent']
    # Exactly one replacement tab claimed the dead instance; nothing is left to report.
    assert outcome['state'] == {'keys': [], 'held': [f"tofu.crash.v1.{outcome['doomed']}"]}
    assert [call['page_instance_id'] for call in calls] == [outcome['doomed']]
