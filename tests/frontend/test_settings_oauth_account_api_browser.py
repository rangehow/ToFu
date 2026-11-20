"""Real Settings and v4 account routes; upstream authorization and vault are synthetic."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from tests.oauth.test_codex_account_flow import bearer

pytestmark = pytest.mark.visual
ACCOUNTS = '/api/v4/oauth/accounts'


@pytest.fixture
def account_upstream(monkeypatch):
    from lib.model_routing.repository import InMemoryModelRoutingRepository
    from lib.oauth import codex
    from lib.oauth.manager._device import _CodexDevice
    from routes.api_v1 import oauth_accounts

    repository = InMemoryModelRoutingRepository()
    monkeypatch.setattr(oauth_accounts, 'ModelRoutingRepository', lambda: repository)

    def exchange(_url, payload, **_kwargs):
        code = payload['code']
        if code == 'synthetic-rejected':
            return SimpleNamespace(status_code=400, text='private synthetic provider diagnostic')
        subject = 'synthetic-work' if code in ('synthetic-work-short', 'synthetic-work-reconnect') else code
        assert subject in ('synthetic-work', 'synthetic-personal')
        return SimpleNamespace(status_code=200, json=lambda: {
            'access_token': bearer(subject), 'id_token': bearer(subject),
            'refresh_token': 'never-render-fixture-refresh-token',
            'expires_in': 60 if code == 'synthetic-work-short' else 3600,
        })

    monkeypatch.setattr(codex, '_oauth_http_post', exchange)
    monkeypatch.setattr(codex, '_save_login_token', lambda *_: pytest.fail('Unexpected singleton write'))
    monkeypatch.setattr(_CodexDevice, 'request', staticmethod(lambda **_kwargs: {
        'device_auth_id': 'private-fixture-device-id', 'user_code': 'ABCD-EFGH', 'interval': 1,
        'expires_in': 300, 'verification_url': 'https://auth.openai.com/codex/device',
    }))
    monkeypatch.setattr(_CodexDevice, 'poll', staticmethod(lambda *_args, **_kwargs: {
        'authorization_code': 'synthetic-personal', 'code_verifier': 'private-fixture-verifier',
    }))
    return repository


def _browser_flow(panel, subject):
    from playwright.sync_api import expect

    flow = panel.locator('.oauth-account-flow')
    flow.get_by_role('button', name='Log in to ChatGPT').click()
    link = flow.get_by_role('link', name='Open verification page')
    expect(link).to_be_visible()
    state = parse_qs(urlparse(link.get_attribute('href')).query)['state'][0]
    field = flow.get_by_role('textbox')
    field.fill('http://localhost:1455/auth/callback?' + urlencode({'code': subject, 'state': 'stale-state'}))
    field.press('Enter')
    expect(flow.get_by_role('status')).to_contain_text('from this sign-in attempt')
    field.fill('http://localhost:1455/auth/callback?' + urlencode({'code': subject, 'state': state}))
    field.press('Enter')
    expect(flow).not_to_be_visible()
    return state


def _button(row, action):
    return row.get_by_role('button', name=f'{action}:', exact=False)


def _rename(row, value):
    from playwright.sync_api import expect

    _button(row, 'Rename').press('Enter')
    field = row.get_by_role('textbox')
    expect(field).to_be_focused()
    field.fill(value)
    field.press('Enter')


@pytest.mark.parametrize('width', [390, 1280])
def test_codex_accounts_settings_wire(browser, live_server, account_upstream, tmp_path, width):
    from playwright.sync_api import expect

    context = browser.new_context(viewport={'width': width, 'height': 900}, service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_ui_lang','en');"
                            "localStorage.setItem('tofu_onboarding_v1_done','1');")
    origin = urlparse(live_server).netloc
    commands, errors, held = [], [], []
    hold = {'reauth': False, 'add': False}

    def intercept(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != origin:
            return route.abort()
        if parsed.path == ACCOUNTS + '/commands':
            command = route.request.post_data_json
            commands.append(command)
            if hold.get(command['action']):
                held.append((route, route.fetch(max_redirects=0)))
                return None
        if parsed.path == '/api/v4/oauth/status':
            return route.fulfill(json={'data': {'codex': {'status': 'not_started'},
                                               'claude': {'status': 'not_started'}}, 'meta': {}})
        if parsed.path.startswith('/api/oauth') or ('/oauth/' in parsed.path and not parsed.path.startswith(ACCOUNTS)):
            return route.abort()
        return route.continue_()

    context.route('**/*', intercept)
    try:
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        if width == 390:
            page.get_by_role('button', name='Toggle sidebar').click()
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        tab = page.locator('.settings-tab[data-tab="oauth"]')
        tab.click()
        tile = page.get_by_role('button', name='ChatGPT', exact=True)
        tile.press('Enter')
        panel = page.locator('#oauthCodexAccounts')
        add = panel.get_by_role('button', name='Add another account')
        rows = panel.get_by_role('listitem')
        expect(add).to_be_visible()
        expect(panel).to_contain_text('No additional accounts linked yet.')
        expect(page.locator('#oauthCodexLoginBtn')).to_be_visible()

        # Escape while the keyboard-selected browser method is preparing must
        # retire only the inline flow, even when the server already accepted it.
        hold['add'] = True
        add.press('Enter')
        flow = panel.locator('.oauth-account-flow')
        with page.expect_request(lambda req: urlparse(req.url).path == ACCOUNTS + '/commands'):
            flow.get_by_role('button', name='Log in to ChatGPT').press('Enter')
        page.keyboard.press('Escape')
        expect(flow).not_to_be_visible()
        expect(page.locator('#settingsModal')).to_be_visible()
        expect(add).to_be_focused()
        route, response = held.pop()
        assert response.ok
        route.fulfill(response=response)
        hold['add'] = False
        expect(add).to_be_enabled()
        assert [command['action'] for command in commands] == ['add']

        add.click()
        state = _browser_flow(panel, 'synthetic-work')
        expect(rows).to_have_count(1)
        assert next(command for command in commands if command['action'] == 'complete')['state'] == state
        _rename(rows.first, 'Work')
        work = rows.filter(has_text='Work')
        expect(work).to_have_count(1)

        add.click()
        panel.get_by_role('button', name='Device code login').click()
        expect(panel.locator('code')).to_have_text('ABCD-EFGH')
        expect(rows).to_have_count(2, timeout=10000)
        _rename(rows.filter(has_not_text='Work'), 'Personal')
        personal = rows.filter(has_text='Personal')
        expect(personal).to_have_count(1)
        a = next(command['account_id'] for command in commands if command.get('alias') == 'Work')
        b = next(command['account_id'] for command in commands if command.get('alias') == 'Personal')
        assert a != b
        _button(personal, 'Set as default').click()
        expect(personal).to_contain_text('Default account')
        expect(work).not_to_contain_text('Default account')
        assert commands[-1]['account_id'] == b
        expect(panel).to_contain_text('does not grant model access')
        expect(personal).to_contain_text('Account quota unavailable')
        assert panel.evaluate('(node) => node.scrollWidth <= node.clientWidth')
        page.locator('#settingsModal .settings-panel').screenshot(path=tmp_path / f'codex-account-api-{width}.png')

        # Another client changes the actual revision. The stale targeted write
        # is not retried; the next deliberate click uses fresh evidence.
        inventory = page.request.get(live_server + ACCOUNTS + '?provider=codex').json()['data']
        receipt = page.request.post(live_server + ACCOUNTS + '/commands', data={
            'provider': 'codex', 'action': 'rename', 'account_id': a,
            'expected_revision': inventory['revision'], 'alias': 'Work (updated)',
        })
        assert receipt.ok
        _rename(work, 'stale alias')
        expect(panel).to_contain_text('Accounts changed.')
        expect(work).to_contain_text('Work (updated)')
        expect(panel).not_to_contain_text('stale alias')
        assert len([command for command in commands if command.get('alias') == 'stale alias']) == 1

        _button(work, 'Sign in again').click()
        _browser_flow(panel, 'synthetic-work')
        expect(_button(work, 'Remove')).to_have_attribute('aria-disabled', 'false')
        assert next(command for command in commands if command['action'] == 'reauth')['account_id'] == a

        # Let the real server acknowledge a start, but deliver it to a retired
        # tab opening. It must not show the old flow or submit any completion.
        hold['reauth'] = True
        _button(work, 'Sign in again').click()
        with page.expect_request(lambda req: urlparse(req.url).path == ACCOUNTS + '/commands'):
            panel.get_by_role('button', name='Log in to ChatGPT').click()
        page.locator('#oauthSubscriptionBack button').click()
        expect(tile).to_be_focused()
        tile.click()
        expect(add).to_be_visible()
        assert len(held) == 1
        route, response = held.pop()
        assert response.ok
        route.fulfill(response=response)
        expect(panel.locator('.oauth-account-flow')).to_have_count(0)
        before = len(commands)
        dialogs = []
        def reject(dialog):
            dialogs.append(dialog.message)
            dialog.dismiss()
        page.once('dialog', reject)
        _button(personal, 'Remove').click()
        assert len(commands) == before and 'Personal' in dialogs[0]
        page.once('dialog', lambda dialog: dialog.accept())
        _button(personal, 'Remove').click()
        expect(rows).to_have_count(1)
        expect(panel).to_contain_text('No default account selected.')
        assert commands[-1]['action'] == 'remove' and commands[-1]['account_id'] == b
        assert 'never-render-fixture-refresh-token' not in panel.inner_text()
        assert not errors
    finally:
        for route, _response in held:
            route.abort()
        context.close()


@pytest.mark.auth_mode('private')
def test_expired_default_reconnect_preserves_account_and_focus(browser, live_server, account_upstream, monkeypatch):
    """Exercise authenticated Settings, the v4 roster, and a one-account clock expiry."""
    import time

    from playwright.sync_api import expect

    from lib.api_keys import create_key
    from lib.oauth import accounts, codex
    from routes.api_v1.auth import SESSION_COOKIE

    now = [time.time()]
    clock = SimpleNamespace(time=lambda: now[0])
    monkeypatch.setattr(accounts, 'time', clock)
    monkeypatch.setattr(codex, 'time', clock)
    _, token = create_key(owner_user_id=1, name='oauth-account-browser', scopes=['admin'])
    context = browser.new_context(service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_ui_lang','en');"
                            "localStorage.setItem('tofu_onboarding_v1_done','1');")
    url = live_server + ACCOUNTS
    origin = urlparse(live_server).netloc
    commands = []

    def intercept(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != origin:
            return route.abort()
        if parsed.path == ACCOUNTS + '/commands':
            commands.append((route.request.post_data_json['action'], route.request.post_data_json.get('account_id')))
        if parsed.path == '/api/v4/update/check':
            return route.abort()
        if parsed.path == '/api/v4/oauth/status':
            return route.fulfill(json={'data': {'codex': {'status': 'not_started'},
                                               'claude': {'status': 'not_started'}}, 'meta': {}})
        if parsed.path.startswith('/api/oauth') or ('/oauth/' in parsed.path and not parsed.path.startswith(ACCOUNTS)):
            return route.abort()
        return route.continue_()

    def command(action, **fields):
        response = context.request.post(url + '/commands', data={'provider': 'codex', 'action': action, **fields})
        assert response.ok, (action, response.status)
        return response.json()['data']

    def inventory():
        response = context.request.get(url + '?provider=codex')
        assert response.ok, response.status
        return response.json()['data']

    def seed(alias, code):
        start = command('add', expected_revision=inventory()['revision'], method='browser')
        state = parse_qs(urlparse(start['auth_url']).query)['state'][0]
        done = command('complete', flow_id=start['flow_id'], code=code, state=state)
        assert done['status'] == 'authorized'
        account_id = done['account_id']
        command('rename', account_id=account_id, expected_revision=inventory()['revision'], alias=alias)
        return account_id

    context.route('**/*', intercept)
    try:
        assert context.request.get(url + '?provider=codex').status == 401
        context.add_cookies([{'name': SESSION_COOKIE, 'value': token, 'url': live_server, 'httpOnly': True}])
        a = seed('Work', 'synthetic-work-short')
        b = seed('Personal', 'synthetic-personal')
        command('default', account_id=a, expected_revision=inventory()['revision'])
        before = inventory()

        page = context.new_page()
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.locator('.settings-tab[data-tab="oauth"]').click()
        page.get_by_role('button', name='ChatGPT', exact=True).press('Enter')
        panel = page.locator('#oauthCodexAccounts')
        work = panel.get_by_role('listitem').filter(has_text='Work')
        personal = panel.get_by_role('listitem').filter(has_text='Personal')
        expect(work).to_contain_text('Authorized')
        expect(work).to_contain_text('Default account')
        expect(personal).to_contain_text('Authorized')

        now[0] += 61
        panel.get_by_role('button', name='Refresh', exact=True).press('Enter')
        expect(work).to_contain_text('Sign-in expired')
        expect(work).to_contain_text('Default account')
        expect(personal).to_contain_text('Authorized')
        expect(personal).not_to_contain_text('Default account')
        expired = inventory()
        assert expired['revision'] == before['revision'] and expired['default_account_id'] == a
        assert {row['account_id']: row['status'] for row in expired['accounts']} == {a: 'needs_reauth', b: 'authorized'}

        reconnect = _button(work, 'Sign in again')
        flow = panel.locator('.oauth-account-flow')

        def submit(code):
            reconnect.press('Enter')
            expect(flow.locator('strong')).to_be_focused()
            page.keyboard.press('Tab')
            expect(flow.get_by_role('button', name='Log in to ChatGPT')).to_be_focused()
            page.keyboard.press('Enter')
            link = flow.get_by_role('link', name='Open verification page')
            expect(link).to_be_visible()
            page.keyboard.press('Tab')
            expect(link).to_be_focused()
            page.keyboard.press('Tab')
            field = flow.get_by_role('textbox')
            expect(field).to_be_focused()
            state = parse_qs(urlparse(link.get_attribute('href')).query)['state'][0]
            field.fill('http://localhost:1455/auth/callback?' + urlencode({'code': code, 'state': state}))
            page.keyboard.press('Enter')
            expect(flow).not_to_be_visible()
            expect(reconnect).to_be_focused()

        submit('synthetic-rejected')
        expect(panel.get_by_role('status').filter(has_text='Sign-in could not be completed')).to_have_text(
            'Sign-in could not be completed. Start a new sign-in attempt.')
        expect(reconnect).to_have_attribute('aria-disabled', 'false')
        expect(panel).not_to_contain_text('private synthetic provider diagnostic')
        expect(work).to_contain_text('Sign-in expired')
        assert inventory() == expired
        assert commands == [('reauth', a), ('complete', None)]

        submit('synthetic-work-reconnect')
        expect(work).to_contain_text('Authorized')
        expect(work).to_contain_text('Default account')
        expect(personal).to_contain_text('Authorized')
        expect(personal).not_to_contain_text('Default account')
        expect(panel).not_to_contain_text('Sign-in could not be completed')
        expect(panel).not_to_contain_text('never-render-fixture-refresh-token')
        expect(reconnect).to_be_focused()
        after = inventory()
        assert after['default_account_id'] == a and after['revision'] == expired['revision'] + 1
        assert after['accounts'] == before['accounts']
        assert commands == [('reauth', a), ('complete', None)] * 2
    finally:
        context.close()


@pytest.mark.auth_mode('private')
@pytest.mark.parametrize('retirement', ['expired', 'cancelled'])
def test_device_reconnect_retires_old_attempt(browser, live_server, account_upstream, monkeypatch, retirement):
    """Only device UI; roster, authentication, expiry and exchange use real app routes."""
    import time

    from playwright.sync_api import expect

    from lib.api_keys import create_key
    from lib.model_routing.repository import OwnerBoundary
    from lib.oauth.accounts import SubscriptionAccounts
    from lib.oauth.manager import account_flow
    from lib.oauth.manager._device import _CodexDevice
    from routes.api_v1.auth import SESSION_COOKIE

    service = SubscriptionAccounts(account_upstream)
    boundary = OwnerBoundary.create(1)
    ids = []
    for revision, subject in enumerate(('synthetic-work', 'synthetic-personal')):
        intent = service.begin(boundary, 'codex', revision * 2)
        account = service.complete(intent, verified_subject=subject,
                                   token={'access_token': bearer(subject), 'account_id': subject},
                                   expires_at=time.time() + (-60 if revision == 0 else 3600))
        service.update(boundary, 'codex', account, revision * 2 + 1,
                       action='rename', alias='Work' if revision == 0 else 'Personal')
        ids.append(account)
    a, b = ids
    service.update(boundary, 'codex', a, 4, action='default')
    monotonic = [time.monotonic()]
    monkeypatch.setattr(account_flow, 'time', SimpleNamespace(monotonic=lambda: monotonic[0]))
    device_requests, provider_polls = [], []
    authorized = [False]

    def device_request(**_kwargs):
        number = len(device_requests) + 1
        device_requests.append(number)
        return {'device_auth_id': f'private-device-{number}', 'user_code': f'SYNTH-{number}',
                'interval': 1, 'expires_in': 300,
                'verification_url': 'https://auth.openai.com/codex/device'}

    def device_poll(device_auth_id, _code, **_kwargs):
        provider_polls.append(device_auth_id)
        if authorized[0]:
            return {'authorization_code': 'synthetic-work-reconnect', 'code_verifier': 'private-fixture-verifier'}
        return None

    monkeypatch.setattr(_CodexDevice, 'request', staticmethod(device_request))
    monkeypatch.setattr(_CodexDevice, 'poll', staticmethod(device_poll))
    _, token = create_key(owner_user_id=1, name='device-account-browser', scopes=['admin'])
    context = browser.new_context(service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_ui_lang','en');"
                            "localStorage.setItem('tofu_onboarding_v1_done','1');")
    origin = urlparse(live_server).netloc
    commands, errors, outside = [], [], []

    def intercept(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != origin:
            outside.append(route.request.url)
            return route.abort()
        if parsed.path == ACCOUNTS + '/commands':
            commands.append(route.request.post_data_json)
        if parsed.path == '/api/v4/update/check':
            return route.abort()
        if parsed.path == '/api/v4/oauth/status':
            return route.fulfill(json={'data': {'codex': {'status': 'not_started'},
                                               'claude': {'status': 'not_started'}}, 'meta': {}})
        if parsed.path.startswith('/api/oauth') or ('/oauth/' in parsed.path and not parsed.path.startswith(ACCOUNTS)):
            return route.abort()
        return route.continue_()

    def inventory():
        response = context.request.get(live_server + ACCOUNTS + '?provider=codex')
        assert response.ok
        return response.json()['data']

    context.route('**/*', intercept)
    try:
        assert context.request.get(live_server + ACCOUNTS + '?provider=codex').status == 401
        context.add_cookies([{'name': SESSION_COOKIE, 'value': token, 'url': live_server, 'httpOnly': True}])
        before = inventory()
        assert before['default_account_id'] == a
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.locator('.settings-tab[data-tab="oauth"]').click()
        page.get_by_role('button', name='ChatGPT', exact=True).press('Enter')
        panel = page.locator('#oauthCodexAccounts')
        rows = panel.get_by_role('listitem')
        work = rows.filter(has_text='Work')
        personal = rows.filter(has_text='Personal')
        reconnect = _button(work, 'Sign in again')
        flow = panel.locator('.oauth-account-flow')
        expect(work).to_contain_text('Sign-in expired')
        expect(work).to_contain_text('Default account')
        expect(personal).to_contain_text('Authorized')
        page.clock.install(time=1_700_000_000_000)
        page.clock.pause_at(1_700_000_001_000)

        def start(number):
            reconnect.press('Enter')
            expect(flow.locator('strong')).to_be_focused()
            page.keyboard.press('Tab')
            expect(flow.get_by_role('button', name='Log in to ChatGPT')).to_be_focused()
            page.keyboard.press('Tab')
            expect(flow.get_by_role('button', name='Device code login')).to_be_focused()
            page.keyboard.press('Enter')
            expect(flow.locator('code')).to_have_text(f'SYNTH-{number}')
            expect(flow.locator('strong')).to_be_focused()
            expect(flow.get_by_role('status')).to_have_text('Waiting for authorization…')
            page.keyboard.press('Tab')
            expect(flow.get_by_role('link', name='Open verification page')).to_be_focused()
            page.keyboard.press('Tab')
            expect(flow.get_by_role('button', name='Cancel', exact=True)).to_be_focused()
            assert commands[-1] == {'provider': 'codex', 'action': 'reauth', 'account_id': a,
                                     'expected_revision': before['revision'], 'method': 'device'}

        start(1)
        with page.expect_response(lambda response: urlparse(response.url).path == ACCOUNTS + '/commands'):
            page.clock.fast_forward(1000)
        expect(flow.get_by_role('button', name='Close', exact=True)).to_be_visible()
        old = commands[-1]['flow_id']
        assert provider_polls == ['private-device-1']
        if retirement == 'expired':
            monotonic[0] += 301
            with page.expect_response(lambda response: response.status == 404 and
                                      urlparse(response.url).path == ACCOUNTS + '/commands'):
                page.clock.fast_forward(1000)
            expect(panel.get_by_role('status').filter(has_text='Sign-in could not be completed')).to_have_text(
                'Sign-in could not be completed. Start a new sign-in attempt.')
        else:
            page.keyboard.press('Escape')
        expect(flow).not_to_be_visible()
        expect(reconnect).to_be_focused()
        expect(page.locator('#settingsModal')).to_be_visible()
        expect(reconnect).to_have_attribute('aria-disabled', 'false')
        assert inventory() == before
        count = len(commands)
        page.clock.fast_forward(5000)
        page.request.get(live_server + ACCOUNTS + '?provider=codex')
        assert len(commands) == count
        assert provider_polls == ['private-device-1']

        start(2)
        assert context.request.get(live_server + ACCOUNTS + '/flows/' + old).status == 404
        authorized[0] = True
        monotonic[0] += 1
        with page.expect_response(lambda response: urlparse(response.url).path == ACCOUNTS + '/commands'):
            page.clock.fast_forward(1000)
        expect(flow).not_to_be_visible()
        expect(reconnect).to_be_focused()
        expect(rows).to_have_count(2)
        expect(work).to_contain_text('Authorized')
        expect(work).to_contain_text('Default account')
        expect(personal).to_contain_text('Authorized')
        expect(personal).not_to_contain_text('Default account')
        expect(panel).not_to_contain_text('Sign-in could not be completed')
        after = inventory()
        assert after['default_account_id'] == a and after['revision'] == before['revision'] + 1
        assert after['accounts'] == [{**row, 'status': 'authorized'} if row['account_id'] == a else row
                                     for row in before['accounts']]
        assert {row['account_id'] for row in after['accounts']} == {a, b}
        assert [command['action'] for command in commands] == (
            ['reauth', 'poll', 'poll', 'reauth', 'poll'] if retirement == 'expired'
            else ['reauth', 'poll', 'reauth', 'poll'])
        assert commands[-1]['flow_id'] != old
        assert provider_polls == ['private-device-1', 'private-device-2']
        count = len(commands)
        page.clock.fast_forward(5000)
        page.request.get(live_server + ACCOUNTS + '?provider=codex')
        assert len(commands) == count
        assert not outside and not errors
        assert not any(command['action'] == 'complete' for command in commands)
        assert 'never-render-fixture-refresh-token' not in panel.inner_text()
    finally:
        context.close()


@pytest.mark.parametrize('default_timing', ['before_commit', 'after_commit'])
def test_second_account_device_completion_and_default_are_isolated(
        browser, live_server, account_upstream, monkeypatch, default_timing):
    """Two ordinary Settings tabs race only against a deterministic fake issuer."""
    from playwright.sync_api import expect

    from lib.llm._sse_core import prepare_request
    from lib.llm.request import dispatch_model_request
    from lib.model_routing.repository import OwnerBoundary
    from lib.oauth.account_dispatch import scoped_account_dispatch
    from lib.oauth.accounts import SubscriptionAccounts
    from lib.oauth.manager import account_flow
    from lib.oauth.manager._device import _CodexDevice

    service = SubscriptionAccounts(account_upstream)
    boundary = OwnerBoundary.create(1)
    from lib.oauth.manager._state import _active_flows
    monkeypatch.setitem(_active_flows, account_flow._key(boundary), {'expires_at': 0})
    monotonic = [1000.0]
    monkeypatch.setattr(account_flow, 'time', SimpleNamespace(monotonic=lambda: monotonic[0]))
    polls = []
    monkeypatch.setattr(_CodexDevice, 'poll', staticmethod(lambda device, *_args, **_kwargs: (
        polls.append(device) or {'authorization_code': 'synthetic-personal',
                                'code_verifier': 'private-fixture-verifier'})))
    context = browser.new_context(service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_ui_lang','en');"
                            "localStorage.setItem('tofu_onboarding_v1_done','1');")
    origin = urlparse(live_server).netloc
    commands, held, outside, errors = [], [], [], []
    hold = [False]

    def intercept(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != origin:
            outside.append(route.request.url)
            return route.abort()
        if parsed.path == ACCOUNTS + '/commands':
            command = route.request.post_data_json
            commands.append(command)
            if command['action'] == 'poll' and hold[0]:
                held.append((route, route.fetch(max_redirects=0)))
                return None
        if parsed.path == '/api/v4/update/check':
            return route.abort()
        if parsed.path == '/api/v4/oauth/status':
            return route.fulfill(json={'data': {'codex': {'status': 'not_started'},
                                               'claude': {'status': 'not_started'}}, 'meta': {}})
        if parsed.path.startswith('/api/oauth') or ('/oauth/' in parsed.path and not parsed.path.startswith(ACCOUNTS)):
            return route.abort()
        return route.continue_()

    def inventory():
        response = context.request.get(live_server + ACCOUNTS + '?provider=codex')
        assert response.ok
        return response.json()['data']

    def open_panel():
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.locator('.settings-tab[data-tab="oauth"]').click()
        page.get_by_role('button', name='ChatGPT', exact=True).click()
        panel = page.locator('#oauthCodexAccounts')
        expect(panel.get_by_role('button', name='Add another account')).to_be_visible()
        return page, panel

    sent = []

    def prepare(binding):
        with scoped_account_dispatch(SimpleNamespace(subscription_account=(binding, account_upstream))):
            return prepare_request({'model': 'fixture', 'messages': [{'role': 'user', 'content': 'hello'}]},
                                   oauth='codex', api_protocol='responses', owner_user_id=1,
                                   base_url='https://chatgpt.com/backend-api/codex')

    def send(plan, subject):
        def transport(prepared):
            sent.append((prepared.hdrs['Authorization'], prepared.hdrs.get('chatgpt-account-id')))
            return 'synthetic-response'
        assert dispatch_model_request(plan, transport, plan) == 'synthetic-response'
        assert sent[-1] == ('Bearer ' + bearer(subject), subject)

    context.route('**/*', intercept)
    try:
        first, panel = open_panel()
        panel.get_by_role('button', name='Add another account').click()
        _browser_flow(panel, 'synthetic-work')
        rows = panel.get_by_role('listitem')
        expect(rows).to_have_count(1)
        _rename(rows.first, 'Work')
        work = rows.filter(has_text='Work')
        a = inventory()['accounts'][0]['account_id']
        if default_timing == 'after_commit':
            _button(work, 'Set as default').click()
            expect(work).to_contain_text('Default account')
        before = inventory()
        sibling = before['accounts'][0]
        binding_a = (service.issued_default(account_upstream.get(boundary), 'codex')
                     if before['default_account_id'] == a else service.bind(boundary, 'codex', a))
        assert binding_a.account_id == a
        plan_a = prepare(binding_a)
        send(plan_a, 'synthetic-work')

        second, other = open_panel()
        first.clock.install(time=1_700_000_000_000)
        first.clock.pause_at(1_700_000_001_000)
        panel.get_by_role('button', name='Add another account').click()
        panel.get_by_role('button', name='Device code login').click()
        flow = panel.locator('.oauth-account-flow')
        expect(flow.locator('code')).to_have_text('ABCD-EFGH')
        pending = commands[-1]
        assert pending == {'provider': 'codex', 'action': 'add', 'method': 'device',
                           'expected_revision': before['revision']}
        assert inventory() == before
        expect(_button(work, 'Remove')).to_have_attribute('aria-disabled', 'true')

        if default_timing == 'before_commit':
            _button(other.get_by_role('listitem').filter(has_text='Work'), 'Set as default').click()
            expect(other.get_by_role('listitem')).to_contain_text('Default account')
            changed = inventory()
            assert changed['default_account_id'] == a
            with first.expect_response(lambda response: response.status == 409 and
                                       urlparse(response.url).path == ACCOUNTS + '/commands'):
                first.clock.fast_forward(1000)
            expect(flow).not_to_be_visible()
            expect(panel).to_contain_text('Accounts changed.')
            assert inventory() == changed
            old = commands[-1]['flow_id']
            assert len(polls) == 1
            count = len(commands)
            first.clock.fast_forward(5000)
            assert len(commands) == count
            send(plan_a, 'synthetic-work')
            panel.get_by_role('button', name='Add another account').click()
            panel.get_by_role('button', name='Device code login').click()
            expect(flow.locator('code')).to_have_text('ABCD-EFGH')
            assert context.request.get(live_server + ACCOUNTS + '/flows/' + old).status == 404
            assert context.request.post(live_server + ACCOUNTS + '/commands', data={
                'provider': 'codex', 'action': 'poll', 'flow_id': old,
            }).status == 404
            assert inventory() == changed and len(polls) == 1
            monotonic[0] += 1

        hold[0] = True
        with first.expect_request(lambda req: urlparse(req.url).path == ACCOUNTS + '/commands'):
            first.clock.fast_forward(1000)
        second.wait_for_function("""async path => {
            const response = await fetch(path);
            const inventory = (await response.json()).data;
            return inventory.accounts.length === 2;
        }""", arg=ACCOUNTS + '?provider=codex')
        assert len(held) == 1
        route, response = held[0]
        assert response.ok
        receipt = response.json()['data']
        assert receipt['status'] == 'authorized' and receipt['account_id'] != a
        assert receipt['flow_id'] == commands[-1]['flow_id']
        b = receipt['account_id']
        committed = inventory()
        assert committed['default_account_id'] == a
        assert next(row for row in committed['accounts'] if row['account_id'] == a) == sibling
        assert {row['account_id'] for row in committed['accounts']} == {a, b}
        other.get_by_role('button', name='Refresh', exact=True).click()
        other_rows = other.get_by_role('listitem')
        expect(other_rows).to_have_count(2)
        personal = other_rows.filter(has_not_text='Work')
        _rename(personal, 'Personal')
        _button(other_rows.filter(has_text='Personal'), 'Set as default').click()
        expect(other_rows.filter(has_text='Personal')).to_contain_text('Default account')
        selected = inventory()
        assert selected['default_account_id'] == b
        binding_b = service.issued_default(account_upstream.get(boundary), 'codex')
        assert binding_b.account_id == b
        plan_b = prepare(binding_b)
        send(plan_a, 'synthetic-work')
        send(plan_b, 'synthetic-personal')
        assert service.bind(boundary, 'codex', a) == binding_a

        route.fulfill(response=response)
        held.pop()
        hold[0] = False
        expect(flow).not_to_be_visible()
        expect(rows).to_have_count(2)
        expect(rows.filter(has_text='Personal')).to_contain_text('Default account')
        expect(work).not_to_contain_text('Default account')
        expect(work).to_contain_text('Authorized')
        expect(panel.get_by_role('button', name='Add another account')).to_be_focused()
        assert inventory() == selected
        send(plan_a, 'synthetic-work')
        send(plan_b, 'synthetic-personal')
        count = len(commands)
        first.clock.fast_forward(5000)
        assert len(commands) == count
        assert len(polls) == (2 if default_timing == 'before_commit' else 1)
        assert [item['action'] for item in commands].count('poll') == len(polls)
        assert not errors and not outside
        assert 'never-render-fixture-refresh-token' not in panel.inner_text()
    finally:
        for route, _response in held:
            route.abort()
        context.close()
