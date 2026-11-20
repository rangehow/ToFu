"""Passive receipt and settings rendering only; no application, grants or effects."""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_page as frontend_visual_page, frontend_visual_origin  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [390, 1180])
@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
def test_owner_delegation_refusal_is_readable_without_hover(frontend_visual_page, tmp_path, width, theme):
    from playwright.sync_api import expect

    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    page.route('**/*generated.json*', lambda route: route.fulfill(json=locale))
    page.evaluate('''theme => {
      localStorage.setItem('tofu_ui_lang', 'en');
      document.documentElement.dataset.theme = theme;
      document.body.dataset.theme = theme;
      document.body.innerHTML = '<main id="fixture"></main><div id="updateLifecycleApprovals"></div>';
    }''', theme)
    entry = tmp_path / 'receipts.ts'
    entry.write_text('\n'.join('export * from ' + json.dumps(str(ROOT / name)) + ';' for name in [
        'frontend/src/features/router-owner-delegation.ts',
        'frontend/src/features/fixtures/owner-delegation-receipts.ts',
        'frontend/src/i18n/index.ts',
    ]))
    page.add_script_tag(path=native_module_path('.native/owner-delegation-receipts.js', entry))
    page.add_style_tag(content='body{display:block;height:auto;overflow:auto;padding:var(--space-4)}'
                                '#fixture{max-width:680px;margin:auto;}')
    page.evaluate('''async () => {
      await ready();
      window.authorityWrites = 0;
      window.views = delegationReceiptProjects.map(fixture => {
        let current = fixture.standing, failRead = false;
        const view = createRouterOwnerDelegation({read: async () => {
          if (failRead) throw new Error('Passive refresh unavailable');
          return current;
        },
          set: async () => { authorityWrites++; throw new Error('Unexpected authority write'); }});
        document.querySelector('#fixture').append(view.settings);
        return {view, revokeReceipt: () => {current = fixture.revoked;},
          failRead: () => {failRead = true;},
          recoverEmpty: () => {failRead = false; current = {...fixture.revoked, actions: []};}};
      });
    }''')
    from jsonschema import Draft202012Validator
    schema = json.loads((ROOT / 'contracts/router_v1.schema.json').read_text())
    validator = Draft202012Validator({**schema, '$ref': '#/$defs/ownerDelegationState'})
    for fixture in page.evaluate('delegationReceiptProjects'):
        validator.validate(fixture['standing'])
        validator.validate(fixture['revoked'])
    disclosures = page.locator('.router-owner-delegation')
    expect(disclosures).to_have_count(2)
    for index in range(2):
        expect(disclosures.nth(index).locator('summary')).to_have_text(locale['routerDelegation.summaryOn'])
    page.evaluate('views.forEach(({revokeReceipt}) => revokeReceipt())')
    for index in range(2):
        disclosure = disclosures.nth(index)
        disclosure.locator('summary').focus()
        disclosure.locator('summary').press('Enter')
        expect(disclosure.locator('summary')).to_have_text(locale['routerDelegation.summaryOff'])
        expect(disclosure.locator('input')).not_to_be_checked()
        expect(disclosure.locator('.router-owner-delegation-last')).to_contain_text(locale['routerDelegation.status.refused'])
        detail = disclosure.locator('.router-owner-delegation-detail')
        expect(detail).to_be_visible()
        expect(detail).to_contain_text('No approval request was created.')
        assert detail.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
        assert disclosure.locator('button').count() == 0
    page.evaluate('views.forEach(({failRead}) => failRead())')
    for index in range(2):
        disclosure = disclosures.nth(index)
        disclosure.locator('summary').press('Enter')
        disclosure.locator('summary').press('Enter')
        expect(disclosure.locator('[role="alert"]')).to_contain_text('Passive refresh unavailable')
        expect(disclosure.locator('summary')).to_contain_text(locale['rich.unknown'])
        expect(disclosure.locator('.router-owner-delegation-last')).not_to_be_visible()
        expect(disclosure.locator('.router-owner-delegation-detail')).not_to_be_visible()
        expect(disclosure.locator('input')).to_be_disabled()
    page.evaluate('views.forEach(({recoverEmpty}) => recoverEmpty())')
    for index in range(2):
        disclosure = disclosures.nth(index)
        disclosure.locator('summary').press('Enter')
        disclosure.locator('summary').press('Enter')
        expect(disclosure.locator('summary')).to_have_text(locale['routerDelegation.summaryOff'])
        expect(disclosure.locator('.router-owner-delegation-last')).to_have_text(locale['routerDelegation.noUse'])
        expect(disclosure.locator('[role="alert"]')).not_to_be_visible()
    page.evaluate('''() => {
      window.initialFailure = createRouterOwnerDelegation({
        read: async () => {throw new Error('Passive initial read unavailable');},
        set: async () => {authorityWrites++; throw new Error('Unexpected authority write');}
      });
      document.querySelector('#fixture').append(initialFailure.settings);
      initialFailure.settings.open = true;
    }''')
    failed = disclosures.nth(2)
    expect(failed.locator('summary')).to_contain_text(locale['rich.unknown'])
    expect(failed.locator('[role="alert"]')).to_contain_text('Passive initial read unavailable')
    expect(failed.locator('.router-owner-delegation-last')).not_to_be_visible()
    expect(failed.locator('input')).to_be_disabled()
    # The existing Settings approval owner, not a delegated refusal, owns pending clicks.
    page.evaluate('''locale => {
      window.t = key => locale[key] || key;
      window.escapeHtml = value => {const span=document.createElement('span');span.textContent=String(value);return span.innerHTML;};
      window._onReady = () => {};
      window.Api = {update: {listLifecycleApprovals: async () => ({records: [
        {id:'synthetic-pending',action:'restart',status:'pending',requested_at:1700000000,
          origin:{conv_id:'synthetic-owner-request'}}
      ]})}};
    }''', locale)
    page.add_script_tag(path=str(ROOT / 'frontend/src/runtime/sections/update.js'))
    page.evaluate('_renderPendingLifecycleApprovals()')
    expect(page.locator('.upd-lc-approve')).to_be_visible()
    expect(page.locator('.upd-lc-deny')).to_be_visible()
    expect(page.locator('.upd-lc-approve')).to_have_text(locale['update.approveExecuteRestart'])
    assert page.evaluate('authorityWrites') == 0
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    page.evaluate('views.forEach(({view}) => view.destroy()); initialFailure.destroy()')
