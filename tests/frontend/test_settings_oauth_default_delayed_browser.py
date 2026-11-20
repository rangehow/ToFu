"""Ordinary default controls against local roster routes, with no issuer or model I/O."""
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual
ACCOUNTS = '/api/v4/oauth/accounts'


def test_default_switch_ignores_retired_roster(browser, live_server, monkeypatch, tmp_path):
    import time

    from playwright.sync_api import expect

    from lib.model_routing.repository import InMemoryModelRoutingRepository, OwnerBoundary
    from lib.oauth.accounts import SubscriptionAccounts
    from routes.api_v1 import oauth_accounts

    repository = InMemoryModelRoutingRepository()
    monkeypatch.setattr(oauth_accounts, 'ModelRoutingRepository', lambda: repository)
    service = SubscriptionAccounts(repository)
    boundary = OwnerBoundary.create(1)
    ids = []
    for alias in ('Work', 'Personal'):
        revision = repository.get(boundary).revision
        intent = service.begin(boundary, 'codex', revision)
        account_id = service.complete(intent, verified_subject=f'synthetic-{alias.lower()}',
                                      token={'access_token': f'synthetic-{alias.lower()}'},
                                      expires_at=time.time() + 3600)
        service.update(boundary, 'codex', account_id, revision + 1, action='rename', alias=alias)
        ids.append(account_id)
    a, b = ids
    service.update(boundary, 'codex', a, repository.get(boundary).revision, action='default')
    original = service.inventory(boundary, 'codex')
    pinned = service.issued_default(repository.get(boundary), 'codex')
    assert pinned.account_id == a
    context = browser.new_context(service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_ui_lang','en');"
                            "localStorage.setItem('tofu_onboarding_v1_done','1');")
    origin = urlparse(live_server).netloc
    held, commands, forbidden, errors = [], [], [], []
    hold = [False]

    def intercept(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != origin:
            forbidden.append(parsed.netloc)
            return route.abort()
        if parsed.path == ACCOUNTS and hold[0]:
            hold[0] = False
            held.append((route, route.fetch(max_redirects=0)))
            return None
        if parsed.path == ACCOUNTS + '/commands':
            command = route.request.post_data_json
            commands.append(command)
            assert command['action'] == 'default'
            return route.continue_()
        if parsed.path == '/api/v4/oauth/status':
            return route.fulfill(json={'data': {'codex': {'status': 'not_started'},
                                               'claude': {'status': 'not_started'}}, 'meta': {}})
        if parsed.path == '/api/v4/oauth/antigravity' and route.request.method == 'GET':
            return route.fulfill(json={'data': {'state': 'missing_cli', 'browser_login_available': False}, 'meta': {}})
        if parsed.path == '/api/v4/update/check':
            return route.abort()
        if parsed.path.startswith('/api/oauth') or ('/oauth/' in parsed.path and parsed.path != ACCOUNTS):
            forbidden.append(parsed.path)
            return route.abort()
        return route.continue_()

    context.route('**/*', intercept)
    try:
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.locator('.settings-tab[data-tab="oauth"]').click()
        tile = page.get_by_role('button', name='ChatGPT', exact=True)
        tile.press('Enter')
        panel = page.locator('#oauthCodexAccounts')
        rows = panel.get_by_role('listitem')
        work = rows.filter(has_text='Work')
        personal = rows.filter(has_text='Personal')
        expect(rows).to_have_count(2)
        expect(work).to_contain_text('Default account')
        expect(personal).not_to_contain_text('Default account')
        for row in (work, personal):
            expect(row).to_contain_text('Authorized')
            expect(row).to_contain_text('Account quota unavailable')

        hold[0] = True
        with page.expect_request(lambda request: urlparse(request.url).path == ACCOUNTS):
            panel.get_by_role('button', name='Refresh', exact=True).press('Enter')
        expect(personal.get_by_role('button', name='Set as default: Personal')).to_have_attribute('aria-disabled', 'true')
        page.wait_for_function("async path => (await (await fetch(path)).json()).data.accounts.length === 2",
                               arg=ACCOUNTS + '?provider=codex')
        assert len(held) == 1
        route, response = held[0]
        assert response.ok and response.json()['data'] == original
        page.locator('#oauthSubscriptionBack button').press('Enter')
        expect(tile).to_be_focused()
        tile.press('Enter')
        expect(panel.get_by_role('button', name='Refresh', exact=True)).to_have_attribute('aria-disabled', 'false')
        personal.get_by_role('button', name='Set as default: Personal').press('Enter')
        expect(personal).to_contain_text('Default account')
        expect(work).not_to_contain_text('Default account')
        selected = service.inventory(boundary, 'codex')
        assert selected == {**original, 'revision': original['revision'] + 1, 'default_account_id': b}
        assert service.issued_default(repository.get(boundary), 'codex').account_id == b
        assert service.bind(boundary, 'codex', a) == pinned
        assert commands == [{'provider': 'codex', 'action': 'default', 'account_id': b,
                             'expected_revision': original['revision']}]

        # The old read settles only after the new mount has changed the default.
        route.fulfill(response=response)
        held.pop()
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        expect(personal).to_contain_text('Default account')
        expect(work).not_to_contain_text('Default account')
        expect(rows).to_have_count(2)
        work.get_by_role('button', name='Set as default: Work').press('Enter')
        expect(work).to_contain_text('Default account')
        expect(personal).not_to_contain_text('Default account')
        final = service.inventory(boundary, 'codex')
        assert final == {**original, 'revision': original['revision'] + 2}
        assert commands[-1] == {'provider': 'codex', 'action': 'default', 'account_id': a,
                               'expected_revision': selected['revision']}
        assert len(commands) == 2
        assert service.bind(boundary, 'codex', a) == pinned
        assert service.issued_default(repository.get(boundary), 'codex') == pinned
        for row in (work, personal):
            expect(row).to_contain_text('Authorized')
            expect(row).to_contain_text('Account quota unavailable')
        assert not forbidden and not errors
        panel.screenshot(path=tmp_path / 'default-switch-delayed-roster.png')
    finally:
        for route, _response in held:
            route.abort()
        context.close()
