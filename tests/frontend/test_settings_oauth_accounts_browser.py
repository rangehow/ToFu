"""Real-browser account presenter candidate with synthetic command ports, not a wire contract."""
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]

FIXTURE = r"""
import {createSubscriptionAccountView} from './frontend/src/features/settings/oauth-account-view';
const copy = {
  "cancel": "Cancel",
  "actionFailed": "Could not confirm the account change. Refresh before retrying.",
  "add": "Add another account",
  "alias": "Alias for {name}",
  "default": "Default account",
  "expired": "Sign-in expired",
  "loadFailed": "Could not check subscription status. Previously shown information may be out of date.",
  "loading": "Checking subscription status…",
  "makeDefault": "Set as default",
  "noneSelected": "No default account selected.",
  "number": "Account {n}",
  "quotaUnavailable": "Account quota unavailable",
  "ready": "Connected",
  "refresh": "Retry status check",
  "relogin": "Sign in again",
  "remove": "Remove",
  "rename": "Rename",
  "unknown": "Status unknown",
  "working": "Account action in progress…",
  "heading": "Subscription accounts",
  "empty": "No additional accounts linked yet.",
  "error": "Error",
  "waitingAuth": "Waiting for authorization…",
  "save": "Save",
  "usageNote": "Counts below are Tofu's local record of requests it dispatched, not provider billing; requests whose account could not be determined are counted as unknown.",
  "usageWindow": "Local record covers {from} – {to}.",
  "usageUnavailable": "Tofu's local usage record is unavailable right now; no counts are shown.",
  "usage": "{requests} requests · {tokens} tokens",
  "usageWithCost": "{requests} requests · {tokens} tokens · ${cost}",
  "lastUsed": "Last used {time}",
  "unknownUsage": "Unknown account: {requests} requests · {tokens} tokens"
};
const workUsage = {requests: 2, tokens: 180, costUsd: 0.03, pricedRequests: 2,
  firstUsedAtMs: 1700000000000, lastUsedAtMs: 1700000005000};
const sample = {selectedId: 'codex-1', accounts: [
  {id: 'codex-1', alias: 'Work', status: 'ready', usage: workUsage},
  {id: 'codex-2', alias: '\u202e<img src=x> Personal', status: 'expired'},
], usage: {source: 'local_record', provider: 'codex',
  accounts: {'codex-1': workUsage},
  unknown: {requests: 1, tokens: 5, costUsd: 0, pricedRequests: 0,
    firstUsedAtMs: 1700000004000, lastUsedAtMs: 1700000004000},
  window: {fromMs: 1700000000000, toMs: 1700000005000}}};
const state = window.accountTest = {calls: [], refreshes: 0, active: true};
function attach() {
  const node = document.createElement('section');
  node.id = 'testAccountInventory';
  document.querySelector('#testAccountInventory')?.remove();
  document.querySelector('#oauthCodexCard').append(node);
  const view = createSubscriptionAccountView(node, {
    text: (key, params) => copy[key].replace(/\{(\w+)\}/g, (_, name) => params[name]),
    isCurrent: () => state.active && node.isConnected,
    add: async () => {state.calls.push(['add']);},
    relogin: id => new Promise((resolve, reject) => {
      state.calls.push(['relogin', id]); state.finish = resolve; state.reject = reject;
    }),
    rename: async (id, alias) => {state.calls.push(['rename', id, alias]);},
    select: async id => {state.calls.push(['select', id]);},
    remove: async id => {state.calls.push(['remove', id]);},
    confirmRemove: name => new Promise(resolve => {
      state.confirmedLabel = name; state.confirm = resolve;
    }),
    refresh: () => {state.refreshes++; view.present(sample);},
  });
  view.present(sample);
}
state.attach = attach;
attach();
"""


@pytest.fixture(scope='module')
def account_fixture_bundle(tmp_path_factory):
    bundle = tmp_path_factory.mktemp('oauth-candidate') / 'fixture.js'
    subprocess.run([str(ROOT / 'node_modules/.bin/esbuild'), '--bundle', '--loader=ts',
                    f'--outfile={bundle}'], input=FIXTURE, cwd=ROOT, text=True, check=True)
    return bundle


@pytest.mark.parametrize('width', [390, 1280])
def test_account_candidate_layout_and_identity(browser, live_server, account_fixture_bundle, tmp_path, width):
    from playwright.sync_api import expect

    context = browser.new_context(viewport={'width': width, 'height': 900}, service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_ui_lang','en');"
                            "localStorage.setItem('tofu_onboarding_v1_done','1');")
    origin = urlparse(live_server).netloc

    def intercept(route):
        request = route.request
        parsed = urlparse(request.url)
        if parsed.netloc != origin:
            return route.abort()
        if parsed.path == '/api/v4/oauth/status':
            return route.fulfill(json={'data': {'codex': {'status': 'not_started'},
                                               'claude': {'status': 'not_started'}}, 'meta': {}})
        if '/oauth/' in parsed.path or parsed.path.startswith('/api/oauth'):
            return route.abort()
        return route.continue_()

    context.route('**/*', intercept)
    try:
        page = context.new_page()
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        if width < 600:
            page.get_by_role('button', name='Toggle sidebar').click()
        page.locator('[data-tofu-action="openSettings()"]').first.click(timeout=6000)
        page.locator('.settings-tab[data-tab="oauth"]').click()
        page.get_by_role('button', name='ChatGPT', exact=True).click()
        expect(page.locator('#oauthCodexLoginBtn')).to_be_visible()
        page.add_script_tag(path=account_fixture_bundle)
        root = page.locator('#testAccountInventory')
        work = root.get_by_role('listitem').filter(has_text='Work')
        personal = root.get_by_role('listitem').filter(has_text='Personal')
        expect(work).to_contain_text('Default account')
        expect(personal).not_to_contain_text('Default account')
        expect(personal).to_contain_text('Sign-in expired')
        expect(personal).to_contain_text('Account quota unavailable')
        expect(work).to_contain_text('2 requests · 180 tokens · $0.0300')
        expect(work).to_contain_text('Last used')
        expect(personal).not_to_contain_text('Last used')
        expect(root).to_contain_text('not provider billing')
        expect(root).to_contain_text('Unknown account: 1 requests · 5 tokens')
        expect(personal.locator('img')).to_have_count(0)
        assert root.evaluate('(e) => e.scrollWidth <= e.clientWidth')
        root.screenshot(path=tmp_path / f'oauth-accounts-{width}.png')

        personal.get_by_role('button', name='Rename: <img src=x> Personal').press('Enter')
        field = personal.get_by_role('textbox', name='Alias for <img src=x> Personal')
        expect(field).to_be_focused()
        field.fill('Personal renamed')
        field.press('Enter')
        page.wait_for_function('accountTest.refreshes === 1')
        personal.get_by_role('button', name='Set as default:', exact=False).click()
        page.wait_for_function('accountTest.refreshes === 2')
        personal.get_by_role('button', name='Remove:', exact=False).click()
        assert page.evaluate('accountTest.confirmedLabel') == '<img src=x> Personal'
        page.evaluate('accountTest.confirm(false)')
        expect(root.get_by_role('button', name='Add another account')).to_have_attribute('aria-disabled', 'false')
        personal.get_by_role('button', name='Remove:', exact=False).click()
        page.evaluate('accountTest.confirm(true)')
        page.wait_for_function('accountTest.refreshes === 3')
        root.get_by_role('button', name='Add another account').click()
        page.wait_for_function('accountTest.refreshes === 4')
        personal.get_by_role('button', name='Sign in again:', exact=False).click()
        expect(root.get_by_role('status')).to_have_text('Account action in progress…')
        expect(work.get_by_role('button', name='Remove:', exact=False)).to_have_attribute('aria-disabled', 'true')
        page.evaluate("accountTest.attach(); accountTest.reject(new Error('private synthetic diagnostic'))")
        expect(work.get_by_role('button', name='Remove:', exact=False)).to_have_attribute('aria-disabled', 'false')
        expect(root).not_to_contain_text('private synthetic diagnostic')
        assert page.evaluate('accountTest.refreshes') == 4
        assert page.evaluate('accountTest.calls') == [
            ['rename', 'codex-2', 'Personal renamed'], ['select', 'codex-2'],
            ['remove', 'codex-2'], ['add'], ['relogin', 'codex-2'],
        ]
    finally:
        context.close()
