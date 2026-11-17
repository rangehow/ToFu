"""Devices page journeys in a real browser: pairing a headless node, changing a permission, a node going offline,
and the Home becoming unreachable and restarting.

The page is the real built application served by the isolated ``live_server``; push frames travel the real
push channel (``lib.agent_core.push.push_event``), exactly as the registry and bridge publish
``devices.changed``. The device registries themselves (contracts desktop_node_v1/desktop_node_registry_v1,
device_routing_v1) are owned by sibling workers; until they land this file stands in for them with a stateful
fake behind ``page.route`` that follows those contracts, so the test pins the UI's side of the contract.
Screenshots go to ``$TOFU_DEVICES_SHOTS`` (else the session screenshot directory).
"""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.visual

OWNER_ID = 1  # The isolated native-app fixture's authenticated owner.
TIERS = ('read', 'write', 'exec', 'gui', 'egress', 'codex_login')
MAC = {'os': 'macOS', 'release': '15.1', 'arch': 'arm64', 'shells': {'available': ['zsh', 'sh'], 'default': 'zsh'}}
WIN = {'os': 'Windows', 'release': '11', 'arch': 'x64', 'shells': {'available': ['pwsh'], 'default': 'pwsh'}}


def _tiers(*on):
    return {tier: tier in on for tier in TIERS}


class FakeDevicesHome:
    """Owner-scoped device registries as the contracts describe them, mutable by the test."""

    def __init__(self):
        self.nodes = {}
        self.ceilings = {}
        self.defaults = {'computer': None, 'server': None, 'browser': None}
        self.writes = []
        self.boot_override = None
        self.code = None

    def node(self, node_id, **fields):
        row = {
            'node_id': node_id, 'name': node_id, 'online': True, 'last_seen': time.time(), 'platform': 'linux',
            'version': '2026.11.15', 'outdated': False, 'tiers': _tiers('read'), 'attachment': 'paired',
            'credential': {'id': f'k_{node_id}', 'name': node_id}, 'state': 'active', 'kind': 'server',
            'route': 'internal', 'rtt_ms': 4, 'running': 0, 'share_roots': [], 'fingerprint': '7F3A-91C2',
            'system': {'os': 'Linux', 'release': '4.18', 'arch': 'x86_64', 'shells': {'available': ['sh'], 'default': 'sh'}},
        }
        row.update(fields)
        self.nodes[node_id] = row
        self.ceilings.setdefault(node_id, _tiers('read'))
        return row

    def permissions(self):
        out = {}
        for node_id, row in self.nodes.items():
            home = self.ceilings.get(node_id, _tiers('read'))
            device = row['tiers'] if row['online'] or row['last_seen'] else None
            out[node_id] = {'node_id': node_id, 'home': home, 'device': device,
                            'effective': {t: bool(home[t] and device and device[t]) for t in TIERS}}
        return out

    def handle(self, route):
        request = route.request
        parts = urlsplit(request.url)
        path, method = parts.path, request.method
        body = json.loads(request.post_data or '{}') if request.post_data else {}

        def ok(data):
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'data': data, 'meta': {'requestId': 'devices-e2e'}}))

        if path == '/api/health':
            response = route.fetch()
            payload = response.json()
            if self.boot_override:
                payload['bootId'] = self.boot_override
            route.fulfill(response=response, body=json.dumps(payload))
            return
        if path == '/api/v4/desktop/nodes' and method == 'GET':
            ok({'nodes': [row for row in self.nodes.values() if row['state'] != 'revoked']})
            return
        if path == '/api/v4/desktop/pair-code' and method == 'POST':
            self.writes.append(('pair-code', body, parse_qs(parts.query)))
            self.code = '482913'
            link = 'https://home.example/proxy/15000/#tofu-pair=s3cret&via=http%3A%2F%2F10.164.3.21%3A15000'
            ok({'code': self.code, 'link': link, 'expires_at': time.time() + 300, 'ttl': 300,
                'command': f"python -m lib.desktop_agent --join '{link}'", 'pending': []})
            return
        if path.startswith('/api/v4/desktop/nodes/'):
            node_id = path.split('/')[5]
            action = path.split('/')[6] if len(path.split('/')) > 6 else ''
            self.writes.append((method, node_id, action, body))
            row = self.nodes[node_id]
            if action == 'confirm':
                row['state'] = 'active'
                row['online'] = True
            elif action == 'reject' or method == 'DELETE':
                row['state'] = 'revoked'
            elif method == 'PATCH':
                row['name'] = body['name']
            ok({'node': row})
            publish()
            return
        if path == '/api/v4/devices/permissions':
            ok({'permissions': self.permissions(), 'rev': 1, 'liveness_window_s': 15})
            return
        if path == '/api/v4/devices/defaults':
            if method == 'PUT':
                self.writes.append(('defaults', body))
                self.defaults.update(body)
                publish()
            ok({'defaults': self.defaults, 'rev': 1})
            return
        if path.startswith('/api/v4/devices/') and path.endswith('/ceiling'):
            node_id = path.split('/')[4]
            self.writes.append(('ceiling', node_id, body))
            self.ceilings[node_id] = {**_tiers('read'), **body['tiers'], 'read': True}
            ok(self.permissions()[node_id])
            publish()
            return
        if path == '/api/v4/desktop/device-waits':
            waits = [{'waitId': 'w1', 'conversationId': 'c1', 'device': {'selector': 'gpu', 'agentId': 'office-win11',
                                                                           'name': 'office-win11', 'key': '0' * 32}}] \
                if 'office-win11' in self.nodes and not self.nodes['office-win11']['online'] else []
            ok({'waits': waits, 'nextCursor': None})
            return
        if path == '/api/v4/browser/status':
            ok({'connected': True, 'clients': [{
                'client_id': 'chrome-1', 'name': 'Chrome', 'profile': 'Work', 'ext_version': '5.9.0',
                'chrome_major': 157, 'last_poll': time.time(), 'seconds_ago': 1.0,
                'capabilities': ['read', 'click', 'fill']}]})
            return
        route.continue_()


def publish():
    from lib.agent_core.push import push_event
    push_event('notify', '__devices__', {'type': 'devices.changed', 'userId': OWNER_ID}, user_id=OWNER_ID)


@pytest.fixture
def shots(screenshot_dir):
    directory = os.environ.get('TOFU_DEVICES_SHOTS') or screenshot_dir
    os.makedirs(directory, exist_ok=True)
    return lambda name: os.path.join(directory, f'{name}.png')


def _open(browser, live_server, home, *, lang='en', theme='light', width=1360, height=900):
    context = browser.new_context(viewport={'width': width, 'height': height})
    context.add_init_script(f"""
        localStorage.setItem('tofu_ui_lang', '{lang}');
        localStorage.setItem('claude_ui_theme', '{theme}');
        localStorage.setItem('tofu:landing', 'chat');
    """)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    for pattern in ('**/api/v4/desktop/**', '**/api/v4/devices/**', '**/api/v4/browser/status', '**/api/health'):
        page.route(pattern, home.handle)
    page.goto(f'{live_server}/#/devices', wait_until='domcontentloaded')
    page.wait_for_function("window.TofuModules && window.TofuModules.version === 3", timeout=15000)
    page.wait_for_function("typeof window.TofuModules?.resolveAction?.('_obClose') === 'function'", timeout=15000)
    page.evaluate("() => window.TofuModules.resolveAction('_obClose')()")
    expect(page.locator('.devices-page')).to_be_visible(timeout=15000)
    return context, page, errors


def settle(page):
    """Screenshots show the resting state: no running open/slide animation and no transient toast."""
    page.evaluate("() => document.querySelectorAll('.ui-toast').forEach(node => node.remove())")
    page.wait_for_function("""() => document.getAnimations().every(animation =>
        animation.playState !== 'running' || animation.effect?.getTiming().iterations === Infinity)""", timeout=5000)


def _row(page, node_id):
    return page.locator(f'[data-device-key="node:{node_id}"]')


def test_pairing_a_headless_node_end_to_end(browser, live_server, shots):
    home = FakeDevicesHome()
    home.node('ruan-macbook', kind='computer', route='browser_relay', rtt_ms=38, platform='darwin',
              system=MAC, tiers=_tiers('read', 'write', 'exec', 'egress'))
    home.ceilings['ruan-macbook'] = _tiers('read', 'write', 'exec', 'gui')
    context, page, errors = _open(browser, live_server, home)
    try:
        pill = page.locator('#sidebarDevicesPill')
        expect(pill).to_contain_text('2/2 online')  # the Mac and the connected browser extension
        page.get_by_role('button', name='Add device').first.click()
        dialog = page.locator('dialog.devices-wizard-shell')
        expect(dialog).to_be_visible()
        dialog.get_by_text('A server or container').click()
        settle(page)
        page.screenshot(path=shots('pairing-1-kind-en-light'))
        dialog.get_by_role('button', name='Next').click()
        expect(dialog.locator('.devices-wizard__command')).to_contain_text('python -m lib.desktop_agent --join')
        expect(dialog.locator('.devices-wizard__digits')).to_have_text('482 913')
        assert home.writes[0][0] == 'pair-code' and home.writes[0][1] == {'kind': 'server'}
        settle(page)
        page.screenshot(path=shots('pairing-2-command-en-light'))

        # The node redeems the code: the registry lists it as pending and publishes devices.changed.
        home.node('gpu-a100-07', state='pending', online=False, tiers=_tiers('read', 'write', 'exec'))
        publish()
        expect(dialog.get_by_text('gpu-a100-07 wants to join')).to_be_visible(timeout=10000)
        expect(dialog.locator('.devices-wizard__fingerprint')).to_have_text('7F3A-91C2')
        settle(page)
        page.screenshot(path=shots('pairing-3-confirm-en-light'))
        dialog.get_by_role('button', name='Confirm').click()
        expect(dialog.get_by_text('Name and allow', exact=True).last).to_be_visible()
        dialog.get_by_text('Developer — read, write, run commands').click()
        dialog.get_by_text('Use as the default for “a server”').click()
        settle(page)
        page.screenshot(path=shots('pairing-4-allow-en-light'))
        dialog.get_by_role('button', name='Done').click()
        expect(dialog).to_be_hidden()
        assert ('POST', 'gpu-a100-07', 'confirm', {'name': 'gpu-a100-07'}) in home.writes
        assert ('ceiling', 'gpu-a100-07', {'tiers': {'write': True, 'exec': True, 'gui': False, 'egress': False,
                                                     'codex_login': False}}) in home.writes
        assert ('defaults', {'server': 'gpu-a100-07'}) in home.writes
        row = _row(page, 'gpu-a100-07')
        expect(row).to_contain_text('online')
        # The rest of the mockup's scenario: an offline desktop with a waiting conversation and an update,
        # and a container whose node ID is presented by two hosts.
        home.node('office-win11', kind='computer', route='direct', online=False, last_seen=time.time() - 14 * 3600,
                  outdated=True, version='2026.11.02', system=WIN, tiers=_tiers('read', 'write', 'exec'))
        home.node('codelab-eval-2', state='conflict', route='direct', tiers=_tiers('read'))
        publish()
        expect(_row(page, 'office-win11')).to_contain_text('conversations waiting: 1', timeout=10000)
        expect(_row(page, 'codelab-eval-2')).to_contain_text('identity conflict')
        expect(row.locator('.devices-chip.ui-badge--success')).to_have_count(3)
        expect(pill).to_contain_text('3/5 online · 1 needs attention')
        settle(page)
        page.screenshot(path=shots('pairing-5-listed-en-light'), full_page=True)
        topology = page.locator('.devices-topology')
        topology.scroll_into_view_if_needed()
        settle(page)
        topology.screenshot(path=shots('topology-en-light'))
        _row(page, 'ruan-macbook').get_by_role('button', name='Details for ruan-macbook').click()
        drawer = page.locator('dialog.devices-drawer-shell')
        expect(drawer.get_by_role('switch', name='Run commands: allowed on this Home')).to_be_checked()
        expect(drawer.get_by_text('off (device)')).to_be_visible()
        settle(page)
        page.screenshot(path=shots('drawer-en-light'))
        page.keyboard.press('Escape')
        assert errors == []
    finally:
        context.close()


def test_permission_change_and_node_going_offline(browser, live_server, shots):
    home = FakeDevicesHome()
    home.node('ruan-macbook', kind='computer', route='browser_relay', rtt_ms=38,
              tiers=_tiers('read', 'write', 'exec', 'egress'))
    home.ceilings['ruan-macbook'] = _tiers('read', 'write', 'exec')
    home.node('office-win11', kind='computer', route='direct', online=False, last_seen=time.time() - 14 * 3600,
              outdated=True, version='2026.11.02', tiers=_tiers('read', 'write', 'exec'))
    context, page, errors = _open(browser, live_server, home, lang='zh', theme='dark')
    try:
        expect(_row(page, 'office-win11')).to_contain_text('14 小时前在线')
        expect(_row(page, 'office-win11')).to_contain_text('等待它的对话：1')
        # Default Home ceiling is read-only: the device's write/exec and the undeclared gui/egress are off here.
        expect(_row(page, 'office-win11').locator('.devices-chip--off')).to_have_count(4)
        expect(_row(page, 'ruan-macbook').locator('.devices-chip--off')).to_have_count(2)
        settle(page)
        page.screenshot(path=shots('fleet-zh-dark'), full_page=True)
        _row(page, 'ruan-macbook').get_by_role('button', name='ruan-macbook 的详情').click()
        drawer = page.locator('dialog.devices-drawer-shell')
        expect(drawer).to_be_visible()
        egress = drawer.get_by_role('switch', name='订阅出网：这个家允许')
        expect(egress).not_to_be_checked()
        egress.check()
        expect(drawer.get_by_role('switch', name='订阅出网：这个家允许')).to_be_checked(timeout=10000)
        assert ('ceiling', 'ruan-macbook', {'tiers': {'write': True, 'exec': True, 'gui': False, 'egress': True,
                                                      'codex_login': False}}) in home.writes
        gui_device = drawer.get_by_label('屏幕、鼠标和剪贴板：设备允许（只能在设备上修改）')
        expect(gui_device).to_have_text('关')
        expect(drawer.locator('tbody td:nth-child(3) input')).to_have_count(0)
        settle(page)
        page.screenshot(path=shots('drawer-permissions-zh-dark'))
        page.keyboard.press('Escape')
        expect(drawer).to_be_hidden()
        page.wait_for_function("location.hash === '#/devices'", timeout=5000)

        # The bridge's liveness watcher publishes devices.changed when the node stops polling.
        home.nodes['ruan-macbook'].update(online=False, last_seen=time.time() - 20)
        publish()
        expect(_row(page, 'ruan-macbook')).to_contain_text('离线', timeout=10000)
        expect(page.locator('#sidebarDevicesPill')).to_contain_text('1/3 在线')
        expect(_row(page, 'ruan-macbook').get_by_role('button', name='通过本页中转')).to_be_visible()
        settle(page)
        page.screenshot(path=shots('node-offline-zh-dark'), full_page=True)
        assert errors == []
    finally:
        context.close()


def test_home_unreachable_then_restarted_is_shown_honestly(browser, live_server, shots):
    home = FakeDevicesHome()
    home.node('gpu-a100-07', tiers=_tiers('read', 'write', 'exec'))
    context, page, errors = _open(browser, live_server, home, theme='tofu')
    try:
        expect(_row(page, 'gpu-a100-07')).to_contain_text('online')
        # Idle-loaded chunks (the pill) must be in before the network drops.
        expect(page.locator('#sidebarDevicesPill')).to_contain_text('online', timeout=15000)
        context.set_offline(True)
        page.evaluate("() => window.dispatchEvent(new Event('offline'))")
        banner = page.locator('.ui-alert--warning', has_text="This Home isn't reachable")
        expect(banner).to_be_visible(timeout=30000)
        expect(page.locator('#sidebarDevicesPill')).to_contain_text('Home unreachable')
        expect(_row(page, 'gpu-a100-07')).to_contain_text('last known: online')
        settle(page)
        page.screenshot(path=shots('home-unreachable-en-tofu'), full_page=True)
        home.boot_override = 'restarted-boot'
        context.set_offline(False)
        page.evaluate("() => window.dispatchEvent(new Event('online'))")
        expect(banner).to_be_hidden(timeout=60000)
        expect(page.locator('.ui-alert--info', has_text='This Home restarted')).to_be_visible(timeout=15000)
        settle(page)
        page.screenshot(path=shots('home-restarted-en-tofu'), full_page=True)
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize('lang', ['en', 'zh'])
def test_narrow_width_and_topology(browser, live_server, shots, lang):
    home = FakeDevicesHome()
    home.node('ruan-macbook', kind='computer', route='browser_relay', tiers=_tiers('read', 'write', 'exec', 'gui'))
    home.node('gpu-a100-07', tiers=_tiers('read', 'write', 'exec'))
    home.node('codelab-eval-2', state='conflict', tiers=_tiers('read'))
    context, page, errors = _open(browser, live_server, home, lang=lang, width=390, height=844)
    try:
        expect(_row(page, 'gpu-a100-07')).to_be_visible()
        expect(page.locator('#sidebar')).to_have_class(re.compile(r'\bcollapsed\b'))
        page.wait_for_timeout(400)  # the drawer's slide-out transition
        width = page.evaluate("() => document.querySelector('.devices-page').scrollWidth")
        assert width <= 390, width
        settle(page)
        page.screenshot(path=shots(f'narrow-{lang}-light'), full_page=True)
        topology = page.locator('.devices-topology')
        topology.scroll_into_view_if_needed()
        expect(topology.locator('.devices-topology__svg')).to_be_visible()
        assert errors == []
    finally:
        context.close()


def test_settings_keeps_only_defaults_and_a_link(browser, live_server, shots):
    home = FakeDevicesHome()
    home.node('gpu-a100-07', tiers=_tiers('read', 'write', 'exec'))
    context, page, errors = _open(browser, live_server, home)
    try:
        page.evaluate("() => window.TofuModules.resolveAction('jumpToSetting')('device-defaults')")
        panel = page.locator('#settingsTab_devices')
        expect(panel).to_be_visible(timeout=15000)
        select = panel.get_by_label('Default for “a server”')
        expect(select).to_be_visible()
        select.select_option('gpu-a100-07')
        page.wait_for_function("() => true")
        expect(page.locator('.ui-toast', has_text='Default device saved.')).to_be_visible(timeout=10000)
        assert ('defaults', {'server': 'gpu-a100-07'}) in home.writes
        # No fleet list, pairing or permission controls remain in Settings.
        expect(panel.locator('table, input[role="switch"]')).to_have_count(0)
        settle(page)
        page.screenshot(path=shots('settings-devices-en-light'))
        panel.get_by_role('button', name='Open the Devices page').click()
        page.wait_for_function("location.hash === '#/devices'")
        expect(page.locator('#settingsModal.open')).to_have_count(0)
        assert errors == []
    finally:
        context.close()


def test_real_home_routes_feed_the_page_without_a_failed_source(browser, live_server):
    """No fake: the isolated Home's own registries (nodes, permissions, defaults, waits, browsers, health)."""
    context = browser.new_context(viewport={'width': 1360, 'height': 900})
    context.add_init_script("localStorage.setItem('tofu_ui_lang', 'en'); localStorage.setItem('tofu:landing', 'chat');")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(f'{live_server}/#/devices', wait_until='domcontentloaded')
        expect(page.locator('[data-device-key="self"]')).to_be_visible(timeout=20000)
        expect(page.locator('.ui-alert--error')).to_have_count(0)
        expect(page.get_by_text("This Home doesn't report its own limits yet")).to_have_count(0)
        status = page.evaluate("""async () => {
          const r = await fetch('api/v4/devices/permissions', {credentials: 'include'});
          return [r.status, Object.keys((await r.json()).data || {}).sort()];
        }""")
        assert status == [200, ['liveness_window_s', 'permissions', 'rev']], status
        assert errors == []
    finally:
        context.close()
