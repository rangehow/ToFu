"""The served subscription directory reveals only the selected provider's detail."""
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [390, 1280])
def test_subscription_directory(browser, live_server, tmp_path, width):
    from playwright.sync_api import expect

    context = browser.new_context(viewport={'width': width, 'height': 980}, service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_ui_lang','en');"
                            "localStorage.setItem('tofu_onboarding_v1_done','1');")
    origin = urlparse(live_server).netloc
    inventory_reads = []

    def success(route, data):
        return route.fulfill(json={'data': data, 'meta': {}})

    def intercept(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != origin:
            return route.abort()
        if parsed.path == '/api/v4/oauth/status':
            return success(route, {
                'claude': {'status': 'not_started'},
                'codex': {'authenticated': True, 'provider_ready': True, 'model_count': 13,
                          'email': 'work@example.test', 'quota': {'source': 'usage_api', 'age_seconds': 0,
                              'primary': {'remaining_percent': 80, 'window_minutes': 10080}}},
                'kimi': {'authenticated': True, 'model_count': 3},
            })
        if parsed.path == '/api/v4/oauth/accounts':
            inventory_reads.append(route.request.method)
            return success(route, {'provider': 'codex', 'revision': 7, 'default_account_id': 'work', 'accounts': [
                {'account_id': 'work', 'alias': 'Work', 'status': 'authorized'},
                {'account_id': 'personal', 'alias': 'Personal', 'status': 'needs_reauth'},
            ]})
        if parsed.path == '/api/v4/oauth/antigravity':
            return success(route, {'state': 'missing_cli'})
        if parsed.path == '/api/v4/providers/catpaw/local-import':
            return success(route, {'available': True, 'connected': True, 'model_count': 51})
        if parsed.path == '/api/v4/providers/templates':
            return success(route, {'items': [
                {'key': 'bailian_coding_plan', 'name': 'Bailian Coding Plan', 'brand': 'qwen', 'category': 'subscription'},
                {'key': 'minimax_plan', 'name': 'MiniMax Coding Plan', 'brand': 'minimax', 'category': 'subscription'},
                {'key': 'usage_based', 'name': 'Pay as you go', 'brand': 'openai', 'category': 'official'},
            ]})
        if parsed.path.startswith(('/api/v4/oauth/', '/api/oauth/')):
            return route.abort()
        return route.continue_()

    context.route('**/*', intercept)
    try:
        page = context.new_page()
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        if width == 390:
            page.get_by_role('button', name='Toggle sidebar').click()
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        tab = page.locator('.settings-tab[data-tab="oauth"]')
        tab.click()
        modal = page.locator('#settingsModal .settings-panel')
        overview = page.locator('#oauthSubscriptionOverview')
        detail = page.locator('#oauthSubscriptionDetail')
        back = page.locator('#oauthSubscriptionBack button')
        chatgpt = overview.get_by_role('button', name='ChatGPT', exact=True)
        plan = overview.get_by_role('button', name='Bailian Coding Plan', exact=True)
        expect(chatgpt).to_contain_text('13')
        expect(overview.get_by_role('button', name='CatPaw (Credit Hub)')).to_contain_text('51')
        expect(overview.get_by_role('button', name='Antigravity')).to_contain_text('CLI not installed')
        expect(overview.get_by_role('button', name='Kimi Code')).to_contain_text('3')
        expect(overview.get_by_role('button', name='Claude')).to_be_visible()
        expect(plan).to_be_visible()
        expect(overview.get_by_role('button', name='Pay as you go')).to_have_count(0)
        expect(overview).not_to_contain_text('work@example.test')
        expect(detail).to_be_hidden()
        assert inventory_reads == []
        assert overview.evaluate('(node) => node.scrollWidth <= node.clientWidth')
        modal.screenshot(path=tmp_path / f'subscriptions-overview-{width}.png')

        chatgpt.press('Enter')
        heading = detail.get_by_role('heading', name='ChatGPT', exact=True)
        expect(heading).to_be_focused()
        assert heading.evaluate("node => node.clientHeight <= Math.ceil(parseFloat(getComputedStyle(node).lineHeight))")
        expect(detail.get_by_text('work@example.test')).to_be_visible()
        expect(detail.get_by_text('80%', exact=False)).to_be_visible()
        expect(detail.get_by_text('Personal', exact=True)).to_be_visible()
        expect(detail.get_by_role('heading', name='Claude')).to_have_count(0)
        expect(overview).to_be_hidden()
        assert inventory_reads == ['GET']
        assert detail.evaluate('(node) => node.scrollWidth <= node.clientWidth')
        modal.screenshot(path=tmp_path / f'subscriptions-chatgpt-{width}.png')
        back.click()
        expect(chatgpt).to_be_focused()

        plan.click()
        expect(detail.get_by_role('heading', name='Bailian Coding Plan')).to_be_focused()
        expect(detail.locator('input[type="password"]:visible')).to_have_count(1)
        expect(detail.get_by_role('heading', name='MiniMax Coding Plan')).to_have_count(0)
        expect(detail.get_by_text('Personal', exact=True)).to_have_count(0)
        assert inventory_reads == ['GET']
        back.click()
        expect(plan).to_be_focused()
        plan.click()
        page.locator('.settings-tab[data-tab="general"]').click()
        tab.click()
        expect(overview).to_be_visible()
        expect(detail).to_be_hidden()
    finally:
        context.close()
