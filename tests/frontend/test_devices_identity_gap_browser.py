"""Inert Devices view: exact service identities are not a physical-device join key."""
import ast
import asyncio
import builtins
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._jsdom import require_node_modules
from tests.support.hermetic_settings import FIXTURE_ORIGIN, settings_fixture_context

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]

FIXTURE = r'''
import { ready } from '#tofu-test-root/frontend/src/i18n';
import { projectFleet } from '#tofu-test-root/frontend/src/features/devices/model';
import { mountDevicesPage } from '#tofu-test-root/frontend/src/features/devices/page';
import '#tofu-test-root/frontend/src/styles/application/00-design-tokens.css';
import '#tofu-test-root/frontend/src/styles/application/30-ui-kit.css';
import '#tofu-test-root/frontend/src/styles/application/38-devices.css';

export async function mount(sources) {
  await ready();
  const before = JSON.stringify(sources);
  const fleet = projectFleet(sources);
  const snapshot = {fleet, sources, loading: false, disconnected: false,
    syncedAt: 100000, restartedAt: null, staleSources: [], ceilingReviews: new Map()};
  const calls = [];
  const forbidden = () => { throw new Error('Fixture permits presentation only'); };
  const store = {snapshot: () => snapshot,
    subscribe: listener => { listener(snapshot); return () => {}; }, refresh: forbidden};
  const view = mountDevicesPage(document.getElementById('devices'), store, {
    backToChat: forbidden, openDevice: key => calls.push(key), openWizard: forbidden,
    ensureRelay: forbidden, now: () => 100000,
  });
  return {calls, rows: fleet.rows, defaults: fleet.defaults,
    unchanged: () => JSON.stringify(sources) === before, dispose: () => view.dispose()};
}
'''


@pytest.fixture(scope='module')
def devices_gap_assets(tmp_path_factory):
    require_node_modules('vite')
    bundle = tmp_path_factory.mktemp('devices-identity-gap') / 'fixture.js'
    subprocess.run([
        'node', 'scripts/vite_test_bundle.mjs', '--bundle', '--loader=ts',
        '--sourcefile=devices-identity-gap.ts', '--format=iife', '--platform=browser',
        '--global-name=DevicesGap', f'--outfile={bundle}',
    ], input=FIXTURE, cwd=ROOT, text=True, check=True, capture_output=True)
    styles = list(bundle.parent.glob('assets/*.css'))
    assert len(styles) == 1
    # No application boot, API, live server, device, transport, or session is started.
    html = '<!doctype html><html lang="en"><title>Devices identity gap</title>' \
           '<link rel="stylesheet" href="/fixture.css"><body><main id="devices"></main>' \
           '<script src="/fixture.js"></script></body></html>'
    return {
        '/inert.html': ('text/html', html),
        '/fixture.js': ('text/javascript', bundle.read_text()),
        '/fixture.css': ('text/css', styles[0].read_text()),
    }


@pytest.fixture(scope='module')
def canonical_sources():
    from jsonschema import Draft202012Validator

    tiers = {tier: tier == 'read' for tier in ('read', 'write', 'exec', 'gui', 'egress', 'codex_login')}
    auth_path = ROOT / 'lib/bridge_auth.py'
    process_key = next(ast.literal_eval(statement.value)
                       for statement in ast.parse(auth_path.read_text()).body
                       if isinstance(statement, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == '_PROCESS_AGENT_KEY_ID'
                               for target in statement.targets))
    assert process_key
    producer_path = ROOT / 'routes/api_v1/desktop.py'
    producer = next(statement for statement in ast.parse(producer_path.read_text()).body
                    if isinstance(statement, ast.FunctionDef) and statement.name == '_node_row')
    dependencies = {
        'lib.desktop.device_policy': SimpleNamespace(permissions_view=lambda *args, **kwargs: {'effective': dict(tiers)}),
        'lib.desktop_wire.node_protocol': SimpleNamespace(granted_tiers=lambda _: dict(tiers)),
    }

    def inert_import(name, *args, **kwargs):
        return dependencies[name]

    # Execute only the current row projector, never route/auth module initialization.
    namespace = {'__builtins__': {**vars(builtins), '__import__': inert_import}}
    exec(compile(ast.Module(body=[producer], type_ignores=[]), str(producer_path), 'exec'), namespace)
    project = namespace['_node_row']
    agent = {'agent_id': 'packaged-node', 'name': 'workstation', 'online': True, 'last_seen': 100,
             'key_id': process_key, 'platform': 'linux', 'version': 'fixture'}
    transport = {'route': None, 'rtt_ms': None, 'running': 0}
    policy = {'owner': 1, 'record': {}, 'rev': 7}
    packaged = project(None, agent, None, transport, policy=policy)
    # Current producer discrepancy, not an intended attachment contract or physical identity proof.
    schema_only = project(None, {key: value for key, value in agent.items() if key != 'key_id'},
                          None, transport, policy=policy)
    assert packaged['attachment'] == 'paired'
    assert schema_only['attachment'] == 'process'
    paired = project({'nodeId': 'paired-node', 'name': 'workstation', 'state': 'active', 'kind': 'computer'},
                     {'online': False, 'last_seen': 50}, {'id': 'credential-paired', 'name': 'workstation'},
                     transport, policy=policy)
    sources = {
        'home': {'state': 'ok', 'value': {'host': 'workstation', 'bootId': 'home-boot', 'version': 'fixture'}},
        'nodes': {'state': 'ok', 'value': [packaged, paired]},
        'browsers': {'state': 'ok', 'value': [
            {'client_id': 'browser-service', 'name': 'workstation', 'profile': 'Work'},
        ]},
        'permissions': {'state': 'absent'},
        'defaults': {'state': 'ok', 'value': {
            'defaults': {'computer': 'paired-node', 'server': None, 'browser': 'browser-service'}, 'rev': 7}},
        'waits': {'state': 'ok', 'value': []},
    }
    for filename, definition, payload in [
        ('desktop_node_v1.schema.json', 'NodeListing', {'nodes': sources['nodes']['value']}),
        ('device_routing_v1.schema.json', 'DefaultsResponse', sources['defaults']['value']),
    ]:
        contract = json.loads((ROOT / 'contracts' / filename).read_text())
        Draft202012Validator({'$ref': f'#/$defs/{definition}', '$defs': contract['$defs']}).validate(payload)
    return sources


@pytest.mark.parametrize('width', [390, 1280])
def test_canonical_identity_gap_keeps_service_status_and_exact_details_targets(
        devices_gap_assets, canonical_sources, monkeypatch, width):
    from playwright.async_api import WebSocketRoute
    from tests.conftest import _ensure_chromium_library_path

    def no_server(*args, **kwargs):
        pytest.fail('Inert Devices fixture must never connect a WebSocket')

    monkeypatch.setattr(WebSocketRoute, 'connect_to_server', no_server)
    _ensure_chromium_library_path()
    asyncio.run(asyncio.wait_for(_browser_journey(devices_gap_assets, canonical_sources, width), timeout=60))


async def _browser_journey(assets, sources, width):
    from playwright.async_api import async_playwright, expect

    # Compatible physical interpretations only: no physical topology is instantiated or sent to the UI.
    same_machine = {'self': 'machine-a', 'node:packaged-node': 'machine-a',
                    'node:paired-node': 'machine-b', 'browser:browser-service': 'machine-a'}
    distinct_machines = dict(zip(same_machine, ('machine-a', 'machine-b', 'machine-c', 'machine-d')))
    keys = list(same_machine)
    assert list(distinct_machines) == keys
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu'])
        try:
            async with settings_fixture_context(browser, assets=assets) as (context, policy):
                await context.add_init_script("localStorage.setItem('tofu_ui_lang', 'en')")
                page = await context.new_page()
                await page.set_viewport_size({'width': width, 'height': 900})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(FIXTURE_ORIGIN + '/inert.html')
                await page.evaluate('async sources => { window.fixture = await DevicesGap.mount(sources); }', sources)
                assert await page.locator('[data-device-key]').evaluate_all(
                    '(elements) => elements.map(element => element.dataset.deviceKey)') == keys
                assert await page.locator('.devices-row').count() == 3
                assert await page.locator('.devices-home').count() == 1
                assert await page.locator('#devices').evaluate('(element) => element.scrollWidth <= element.clientWidth')
                await expect(page.locator('[data-device-key="node:packaged-node"] .devices-status__main')).to_have_text('online')
                await expect(page.locator('[data-device-key="node:paired-node"] .devices-status__main')).to_have_text('offline')
                await expect(page.locator('[data-device-key="browser:browser-service"] .devices-status__main')).to_have_text('online')
                for key in keys:
                    button = page.locator('[data-device-key=' + json.dumps(key) + '] .devices-details-button')
                    await button.focus()
                    await button.press('Enter')
                assert await page.evaluate('fixture.calls') == keys
                rows = await page.evaluate('fixture.rows')
                by_key = {row['key']: row for row in rows}
                assert by_key['node:packaged-node']['nodeId'] == 'packaged-node'
                assert by_key['node:packaged-node']['inProcess'] is False
                assert by_key['node:packaged-node']['route'] == ''
                assert by_key['node:paired-node']['route'] == ''
                assert by_key['node:paired-node']['rttMs'] is None
                assert by_key['node:paired-node']['permissions'] is None
                assert by_key['node:paired-node']['credentialId'] == 'credential-paired'
                assert by_key['browser:browser-service']['nodeId'] == 'browser-service'
                assert await page.evaluate('fixture.defaults') == sources['defaults']['value']['defaults']
                assert await page.evaluate('fixture.unchanged()')
                await page.evaluate('fixture.dispose()')
                for failed, survivors, notices in [
                    (('home',), keys[1:], ['Home status']),
                    (('nodes',), ['self', 'browser:browser-service'], ['devices']),
                    (('browsers',), keys[:-1], ['browsers']),
                    (('home', 'browsers'), ['node:packaged-node', 'node:paired-node'], ['Home status', 'browsers']),
                ]:
                    mixed = {**sources, **{key: {'state': 'failed', 'message': 'synthetic unavailable'} for key in failed}}
                    await page.evaluate('async sources => { window.fixture = await DevicesGap.mount(sources); }', mixed)
                    assert await page.evaluate('fixture.rows') == [by_key[key] for key in survivors]
                    assert await page.locator('[data-device-key]').evaluate_all(
                        '(elements) => elements.map(element => element.dataset.deviceKey)') == survivors
                    await expect(page.locator('.ui-alert--error')).to_contain_text("Couldn't load")
                    for notice in notices:
                        await expect(page.locator('.ui-alert--error')).to_contain_text(notice)
                    if 'home' in failed:
                        assert await page.locator('.devices-home[data-device-key]').count() == 0
                        assert await page.locator('.devices-home .devices-status, .devices-home .devices-details-button').count() == 0
                        await expect(page.locator('.devices-home__host')).to_have_text('tofu-settings.invalid')
                    if 'browsers' in failed:
                        assert await page.locator('#devicesGroup-browser').count() == 0
                    for key in survivors:
                        await expect(page.locator('[data-device-key=' + json.dumps(key) + '] .devices-status__main')).to_have_text(by_key[key]['state'])
                        button = page.locator('[data-device-key=' + json.dumps(key) + '] .devices-details-button')
                        await button.focus()
                        await button.press('Enter')
                    assert await page.evaluate('fixture.calls') == survivors
                    assert await page.evaluate('fixture.defaults') == sources['defaults']['value']['defaults']
                    assert await page.evaluate('fixture.unchanged()')
                    await page.evaluate('fixture.dispose()')
                unknown = {**sources, 'nodes': {'state': 'absent'}, 'browsers': {'state': 'absent'},
                           'home': {'state': 'failed', 'message': 'synthetic unavailable'}}
                await page.evaluate('async sources => { window.fixture = await DevicesGap.mount(sources); }', unknown)
                assert await page.evaluate('fixture.rows') == []
                assert await page.locator('[data-device-key]').count() == 0
                assert await page.locator('.devices-home .devices-status').count() == 0
                await expect(page.locator('.ui-alert--error')).to_contain_text("Couldn't load Home status")
                assert await page.evaluate('fixture.unchanged()')
                await page.evaluate('fixture.dispose()')
                assert await page.locator('.devices-page').count() == 0
                assert errors == []
                assert policy.denied == []
                assert policy.websockets == []
                assert sorted(policy.allowed) == sorted(('GET', path) for path in assets)
            assert browser.contexts == []
        finally:
            await browser.close()
