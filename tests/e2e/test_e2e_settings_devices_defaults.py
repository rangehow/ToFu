"""Real Settings controls against a mock device policy authority."""
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.visual
DEFAULTS = '/api/v4/devices/defaults'


class MockDevicePolicy:
    def __init__(self):
        self.defaults = {'computer': None, 'server': 'old-server', 'browser': None}
        self.calls = []
        self.lose_ack = False
        self.fail_read = False

    def handle(self, route):
        request = route.request
        path = urlsplit(request.url).path
        status = 200
        if path == DEFAULTS:
            patch = request.post_data_json if request.method == 'PUT' else None
            self.calls.append((request.method, patch))
            data = {'defaults': self.defaults, 'rev': 1}
            if patch is not None:
                self.defaults.update(patch)
                data['execution'] = 'not_split'
                if self.lose_ack:
                    self.lose_ack = False
                    status = 503
            elif self.fail_read:
                status = 503
        elif path == '/api/v4/desktop/nodes':
            data = {'nodes': [{'node_id': name, 'name': name, 'kind': 'server', 'state': 'active',
                               'online': True, 'tiers': {}} for name in ('old-server', 'new-server')]}
        elif path == '/api/v4/desktop/device-waits':
            data = {'waits': []}
        elif path == '/api/v4/browser/status':
            data = {'clients': []}
        else:
            route.fallback()
            return
        if status == 503:
            code = 'device_policy_write_uncertain' if request.method == 'PUT' else 'device_policy_unavailable'
            route.fulfill(status=503, content_type='application/problem+json', json={
                'type': f'urn:tofu:problem:{code}', 'title': 'Unavailable', 'status': 503,
                'detail': 'Mock authority unavailable', 'instance': path, 'code': code, 'requestId': 'defaults-e2e',
            })
        else:
            route.fulfill(json={'data': data, 'meta': {'requestId': 'defaults-e2e'}})


@pytest.fixture
def settings_devices(browser, live_server):
    policy = MockDevicePolicy()
    context = browser.new_context(viewport={'width': 1360, 'height': 900})
    try:
        context.add_init_script("localStorage.setItem('tofu_ui_lang', 'en'); localStorage.setItem('tofu:landing', 'chat');")
        page = context.new_page()
        for pattern in ('**/api/v4/desktop/**', f'**{DEFAULTS}', '**/api/v4/browser/status'):
            page.route(pattern, policy.handle)
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function("typeof window.TofuModules?.resolveAction?.('_obClose') === 'function'")
        page.evaluate("() => window.TofuModules.resolveAction('_obClose')()")
        page.evaluate("() => window.TofuModules.resolveAction('jumpToSetting')('device-defaults')")
        panel = page.locator('#settingsTab_devices')
        select = panel.get_by_label('Default for “a server”')
        expect(select).to_have_value('old-server')
        policy.calls.clear()  # Startup may also fetch defaults for the sidebar.
        yield page, panel, select, policy
    finally:
        context.close()


def test_uncertain_default_save_requires_authority_before_editing(settings_devices):
    page, panel, select, policy = settings_devices
    policy.lose_ack = True
    policy.fail_read = True
    select.select_option('new-server')

    expect(panel.get_by_role('alert')).to_contain_text('The current default is unknown')
    expect(select).to_be_disabled()
    expect(select).to_have_value('')
    expect(panel.get_by_label('Default for “my computer”')).to_be_enabled()
    assert policy.defaults['server'] == 'new-server'
    assert policy.calls == [('PUT', {'server': 'new-server'}), ('GET', None)]
    expect(page.locator('.ui-toast--success')).to_have_count(0)

    retry = panel.get_by_role('button', name='Retry reading default')
    policy.fail_read = False
    retry.click()
    expect(select).to_have_value('new-server')
    expect(select).to_be_enabled()
    expect(retry).to_have_count(0)
    expect(page.locator('.ui-toast--success')).to_have_count(0)
    assert policy.calls == [('PUT', {'server': 'new-server'}), ('GET', None), ('GET', None)]


def test_ordinary_default_save_uses_write_acknowledgement(settings_devices):
    page, panel, select, policy = settings_devices
    select.select_option('new-server')
    expect(page.locator('.ui-toast', has_text='Default device saved.')).to_be_visible()
    expect(select).to_have_value('new-server')
    expect(select).to_be_enabled()
    expect(panel.get_by_role('alert')).to_have_count(0)
    assert policy.calls == [('PUT', {'server': 'new-server'})]
