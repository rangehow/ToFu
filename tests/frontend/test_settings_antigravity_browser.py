"""Exercise the one-off Antigravity card with intercepted v4 responses, never Google."""
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual

ANTIGRAVITY_PATH = '/api/v4/oauth/antigravity'
LOGIN_PATH = ANTIGRAVITY_PATH + '/login'


def test_antigravity_subscription_login_prompt_and_retirement(browser, live_server):
    from playwright.sync_api import expect

    context = browser.new_context(service_workers='block')
    context.add_init_script("""localStorage.setItem('tofu_ui_lang','en');
        localStorage.setItem('tofu_onboarding_v1_done','1');""")
    page = context.new_page()
    status = 'missing_cli'
    reads, prompts, logins = [], [], []
    browser_login_available = False
    login_outcome = None
    reply = '<strong>Literal output</strong>\nsecond line'

    def handle(route):
        request = route.request
        parsed = urlparse(request.url)
        if parsed.netloc != urlparse(live_server).netloc:
            return route.abort()
        if parsed.path == ANTIGRAVITY_PATH + '/install':
            return route.fulfill(json={'data': {'provider': 'antigravity', 'status': 'failed'}, 'meta': {}})
        if parsed.path == LOGIN_PATH:
            assert request.method == 'POST' and request.post_data is None
            logins.append(login_outcome)
            if login_outcome is None:
                return None
            return route.fulfill(json={'data': {'state': login_outcome}, 'meta': {}})
        if parsed.path != ANTIGRAVITY_PATH:
            return route.continue_()
        if request.method == 'GET':
            reads.append(status)
            if status is None:
                return None  # Keep the read pending when leaving the tab.
            return route.fulfill(json={'data': {'state': status,
                                                'browser_login_available': browser_login_available}, 'meta': {}})
        assert request.method == 'POST'
        payload = request.post_data_json
        prompts.append(payload)
        if payload == {'prompt': 'invalid'}:
            return route.fulfill(status=400, json={'error': {'code': 'invalid_prompt',
                'message': 'SYNTHETIC_PRIVATE_DIAGNOSTIC'}, 'meta': {}})
        if payload == {'prompt': 'pending prompt'}:
            return None  # Keep the prompt pending when closing Settings.
        return route.fulfill(json={'data': {'state': 'ready', 'text': reply}, 'meta': {}})

    def antigravity_request(request):
        return urlparse(request.url).path == ANTIGRAVITY_PATH

    context.route('**/*', handle)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        opening = page.locator('[data-tofu-action="openSettings()"]').first
        opening.click()
        modal = page.locator('#settingsModal')
        expect(modal).to_be_visible(timeout=20000)
        assert reads == []
        oauth = page.locator('.settings-tab[data-tab="oauth"]')
        oauth.click()
        modal.get_by_role('button', name='Antigravity', exact=True).click()
        card = modal.get_by_role('region', name='Antigravity')
        badge = card.get_by_role('status')
        prompt = card.get_by_role('textbox', name='Prompt')
        refresh = card.get_by_role('button', name='Refresh status')
        send = card.get_by_role('button', name='Send to Google')
        login = card.get_by_role('button', name='Sign in with Google')
        cancel = card.get_by_role('button', name='Cancel', exact=True)
        expect(badge).to_have_text('Antigravity CLI not installed')
        expect(card).to_contain_text('https://antigravity.google/cli/install.sh')
        expect(card).to_contain_text('same OS user')
        expect(card).to_contain_text('private Tofu host')
        expect(card).to_contain_text('Google handles sign-in and consent')
        expect(prompt).not_to_be_visible()

        status = 'login_required'
        refresh.click()
        expect(badge).to_have_text('Not logged in')
        expect(prompt).not_to_be_visible()
        expect(login).not_to_be_visible()
        browser_login_available = True
        refresh.click()
        expect(login).to_be_visible()
        expect(card).to_contain_text('on the Tofu host')
        assert not logins  # Opening/refreshing the tab does not start sign-in.
        with page.expect_request(lambda req: urlparse(req.url).path == LOGIN_PATH):
            login.click()
        expect(badge).to_have_text('Waiting for authorization…')
        expect(prompt).not_to_be_visible()
        with page.expect_event('requestfailed', predicate=lambda req: urlparse(req.url).path == LOGIN_PATH):
            cancel.click()
        expect(badge).to_have_text('Not logged in')
        expect(login).to_be_visible()
        login_outcome = 'ready'
        login.click()
        expect(badge).to_have_text('Ready')
        expect(login).not_to_be_visible()
        assert logins == [None, 'ready']
        expect(prompt).to_be_visible()
        expect(send).to_be_disabled()
        expect(card).to_contain_text('one-off text prompt to Google')
        expect(card).not_to_contain_text('MCP')
        assert reads == ['missing_cli', 'login_required', 'login_required']

        prompt.fill('invalid')
        send.click()
        expect(card.get_by_role('alert')).to_contain_text('This prompt was rejected')
        expect(card).not_to_contain_text('SYNTHETIC_PRIVATE_DIAGNOSTIC')
        prompt.fill('  a single text prompt\nwith another line')
        send.click()
        expect(card.get_by_text(reply, exact=True)).to_be_visible()
        expect(card.locator('strong')).to_have_count(0)
        assert prompts == [{'prompt': 'invalid'}, {'prompt': '  a single text prompt\nwith another line'}]

        status = None
        refresh.click()
        expect(badge).to_have_text('Loading status…')
        with page.expect_event('requestfailed', predicate=antigravity_request) as cancelled_read:
            page.locator('.settings-tab[data-tab="general"]').click()
        assert cancelled_read.value.method == 'GET'
        assert cancelled_read.value.failure == 'net::ERR_ABORTED'
        expect(card).to_have_count(0)

        status = 'ready'
        oauth.click()
        modal.get_by_role('button', name='Antigravity', exact=True).click()
        expect(prompt).to_have_value('')
        expect(card).not_to_contain_text(reply)
        prompt.fill('pending prompt')
        with page.expect_request(antigravity_request):
            send.click()
        expect(badge).to_have_text('Sending to Google…')
        with page.expect_event('requestfailed', predicate=antigravity_request) as cancelled_prompt:
            modal.locator('.settings-close-btn').click()
        assert cancelled_prompt.value.method == 'POST'
        assert cancelled_prompt.value.failure == 'net::ERR_ABORTED'
        expect(modal).not_to_be_visible()
        expect(card).to_have_count(0)

        status = 'unsupported'
        opening.click()
        oauth.click()
        modal.get_by_role('button', name='Antigravity', exact=True).click()
        expect(badge).to_have_text('Unsupported on this host')
        expect(prompt).not_to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('outcome', ['success', 'failure', 'retired'])
def test_antigravity_auto_install_on_selected_detail(browser, live_server, tmp_path, outcome):
    from playwright.sync_api import expect

    context = browser.new_context(viewport={'width': 1280, 'height': 950}, service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_ui_lang','en');"
                            "localStorage.setItem('tofu_onboarding_v1_done','1');")
    page = context.new_page()
    held, calls = [], []
    state = {'installed': False}

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != urlparse(live_server).netloc:
            return route.abort()
        if parsed.path == ANTIGRAVITY_PATH + '/install':
            calls.append('install')
            assert route.request.method == 'POST' and not route.request.post_data
            held.append(route)
            return None
        if parsed.path == ANTIGRAVITY_PATH:
            assert route.request.method == 'GET', 'Installation must not start sign-in or send a prompt'
            return route.fulfill(json={'data': {'state': 'login_required' if state['installed'] else 'missing_cli',
                                                'browser_login_available': state['installed']}, 'meta': {}})
        return route.continue_()

    context.route('**/*', handle)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.locator('.settings-tab[data-tab="oauth"]').click()
        tile = page.get_by_role('button', name='Antigravity', exact=True)
        expect(tile).to_contain_text('not installed')
        assert calls == []
        tile.press('Enter')
        card = page.get_by_role('region', name='Antigravity')
        expect(card.get_by_role('status')).to_have_text('Installing the Antigravity CLI…')
        expect(card.get_by_role('button', name='Refresh status')).to_be_disabled()
        assert calls == ['install']
        if outcome == 'retired':
            page.locator('#settingsModal .settings-close-btn').click()
        state['installed'] = outcome != 'failure'
        held.pop().fulfill(json={'data': {'provider': 'antigravity',
            'status': 'failed' if outcome == 'failure' else 'installed',
            'executable': '/home/demo/.local/bin/agy', 'version': '1.2.3'}, 'meta': {}})
        if outcome == 'retired':
            page.locator('[data-tofu-action="openSettings()"]').first.click()
            page.locator('.settings-tab[data-tab="oauth"]').click()
            tile.press('Enter')
            expect(card.get_by_role('button', name='Sign in with Google')).to_be_visible()
            expect(card).not_to_contain_text('Installed the Antigravity CLI')
        elif outcome == 'failure':
            expect(card.get_by_role('button', name='Retry install')).to_be_visible()
            expect(card).to_contain_text('Automatic install did not finish')
            card.get_by_role('button', name='Retry install').click()
            expect(card.get_by_role('status')).to_have_text('Installing the Antigravity CLI…')
            assert calls == ['install', 'install']
            state['installed'] = True
            held.pop().fulfill(json={'data': {'provider': 'antigravity', 'status': 'installed',
                'executable': '/home/demo/.local/bin/agy', 'version': '1.2.3'}, 'meta': {}})
            expect(card.get_by_role('button', name='Sign in with Google')).to_be_visible()
        else:
            expect(card).to_contain_text('Installed the Antigravity CLI 1.2.3 at /home/demo/.local/bin/agy')
            expect(card.get_by_role('button', name='Sign in with Google')).to_be_visible()
            page.locator('#settingsModal .settings-panel').screenshot(path=tmp_path / 'antigravity-installed.png')
            page.locator('#oauthSubscriptionBack button').click()
            tile.click()
            assert calls == ['install']
    finally:
        for route in held:
            route.abort()
        context.close()
