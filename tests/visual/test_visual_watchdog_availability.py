"""Synthetic health-check ports only; no application, real ensures or navigation."""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_page as frontend_visual_page, frontend_visual_origin  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [390, 1180])
@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
def test_watchdog_withdraws_stale_open_and_restores_fresh_state(frontend_visual_page, tmp_path, width, theme):
    from jsonschema import Draft202012Validator
    from playwright.sync_api import expect

    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    page.route('**/*generated.json*', lambda route: route.fulfill(json=locale))
    page.evaluate('''theme => {
      localStorage.setItem('tofu_ui_lang', 'en');
      document.documentElement.dataset.theme = theme;
      document.body.dataset.theme = theme;
      document.body.innerHTML = '<main id="fixture"></main><div id="header"></div>';
    }''', theme)
    entry = tmp_path / 'watchdog-availability.ts'
    entry.write_text('\n'.join('export * from ' + json.dumps(str(ROOT / name)) + ';' for name in [
        'frontend/src/features/router-watchdog.ts',
        'frontend/src/api/errors.ts',
        'frontend/src/i18n/index.ts',
    ]))
    page.add_script_tag(path=native_module_path('.native/watchdog-availability.js', entry))
    page.add_style_tag(content='body{display:block;height:auto;overflow:auto;padding:var(--space-4)}'
                                '#fixture{max-width:680px;margin:auto;}')
    page.evaluate('''async () => {
      await ready();
      window.states = {
        standing: {projectPath:'/synthetic-owner/health-checks', conversationId:'old-watchdog', revision:1,
          availability:'available', capabilities:{assign:false,sendMessage:false,steerSubagent:true},
          review:{enabled:true,schedule:'7,37 * * * *',taskId:'synthetic-review'}},
      };
      states.unavailable = {...states.standing,revision:2,availability:'unavailable'};
      states.unbound = {...states.standing,conversationId:'',revision:0,availability:'unbound',
        review:{...states.standing.review,enabled:false,taskId:null}};
      states.fresh = {...states.standing,conversationId:'fresh-watchdog',revision:3,
        review:{...states.standing.review,enabled:false}};
      window.readAnswer = states.standing;
      window.ensureCalls = 0;
      window.opened = [];
      window.view = createRouterWatchdogEntry({
        read: () => Promise.resolve(readAnswer),
        ensure: async () => {ensureCalls++; throw new Error('Unexpected synthetic ensure');},
        open: id => opened.push(id), back: () => {}, activeConversationId: () => 'old-watchdog',
        notifyError: message => {throw new Error(message);},
      }, {header:document.querySelector('#header')});
      document.querySelector('#fixture').append(view.settings);
      window.deferRefresh = () => {
        window.readAnswer = new Promise((_resolve, reject) => {window.rejectRead = reject;});
        view.reload();
      };
      window.restore = () => {readAnswer = states.fresh; view.reload();};
    }''')
    schema = json.loads((ROOT / 'contracts/router_v1.schema.json').read_text())
    validator = Draft202012Validator({**schema, '$ref': '#/$defs/watchdogState'})
    for state in page.evaluate('Object.values(states)'):
        validator.validate(state)
    summary = page.locator('.router-watchdog summary')
    settings = page.locator('.router-watchdog')
    action = settings.get_by_role('button', name=locale['routerWatchdog.open'], include_hidden=True)
    expect(summary).to_have_text(locale['routerWatchdog.summaryOn'])
    summary.focus()
    summary.press('Enter')
    expect(action).to_be_visible()
    page.evaluate('deferRefresh()')
    expect(summary).to_contain_text(locale['common.loading'])
    expect(action).not_to_be_visible()
    expect(action).to_be_disabled()
    expect(page.locator('.router-watchdog-header')).not_to_be_visible()
    for status, code, label in [(503, 'service_unavailable', 'rich.unknown'), (403, 'forbidden', 'err.k.permission.chip')]:
        page.evaluate('options => rejectRead(new ApiError("Request failed", options))', {'status': status, 'code': code})
        expect(summary).to_contain_text(locale[label])
        expect(summary).not_to_have_text(locale['routerWatchdog.summaryOn'])
        expect(settings.get_by_role('alert')).to_contain_text('Request failed')
        expect(settings.get_by_role('alert')).to_contain_text(f'HTTP {status}')
        expect(settings.get_by_role('alert')).to_contain_text(code)
        expect(action).not_to_be_visible()
        expect(action).to_be_disabled()
        page.evaluate('''() => {
          window.dispatchEvent(new Event('tofu:language-change'));
          view.settings.querySelector('button').click();
        }''')
        expect(summary).to_contain_text(locale[label])
        assert page.evaluate('opened') == []
        assert page.evaluate('ensureCalls') == 0
        page.evaluate('restore()')
        expect(summary).to_have_text(locale['routerWatchdog.summaryFaultsOnly'])
        expect(settings.get_by_role('alert')).not_to_be_visible()
        expect(action).to_be_visible()
        expect(action).to_be_enabled()
        if status == 503:
            page.evaluate('deferRefresh()')
    page.evaluate('''() => {readAnswer = states.unavailable; view.reload();}''')
    expect(summary).to_contain_text(locale['rich.unknown'])
    expect(summary).not_to_have_text(locale['routerWatchdog.summaryOff'])
    expect(settings.get_by_role('alert')).to_contain_text('unavailable')
    expect(action).not_to_be_visible()
    page.evaluate('view.settings.querySelector("button").click()')
    assert page.evaluate('opened') == []
    assert page.evaluate('ensureCalls') == 0
    for status, code, label in [(503, 'service_unavailable', 'rich.unknown'), (403, 'forbidden', 'err.k.permission.chip')]:
        page.evaluate('deferRefresh()')
        expect(summary).to_contain_text(locale['common.loading'])
        expect(action).not_to_be_visible()
        expect(action).to_be_disabled()
        page.evaluate('view.settings.querySelector("button").click()')
        assert page.evaluate('ensureCalls') == 0
        page.evaluate('options => rejectRead(new ApiError("Request failed", options))', {'status': status, 'code': code})
        expect(summary).to_contain_text(locale[label])
        expect(settings.get_by_role('alert')).to_contain_text(f'HTTP {status}')
        expect(settings.get_by_role('alert')).to_contain_text(code)
        expect(action).not_to_be_visible()
        expect(action).to_be_disabled()
        page.evaluate('view.settings.querySelector("button").click()')
        assert page.evaluate('opened') == []
        assert page.evaluate('ensureCalls') == 0
    page.evaluate('restore()')
    expect(summary).to_have_text(locale['routerWatchdog.summaryFaultsOnly'])
    expect(action).to_be_visible()
    action.focus()
    action.press('Enter')
    assert page.evaluate('opened') == ['fresh-watchdog']
    assert page.evaluate('ensureCalls') == 0
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    page.evaluate('view.destroy()')
    page.evaluate('''() => {
      window.raceRead = states.unbound;
      window.raceOpened = []; window.raceErrors = []; window.raceEnsureCalls = 0;
      window.raceView = createRouterWatchdogEntry({
        read: async () => raceRead,
        ensure: () => {raceEnsureCalls++; return new Promise(resolve => {window.finishEnsure = resolve;});},
        open: id => raceOpened.push(id), back: () => {}, activeConversationId: () => 'main-coordinator',
        notifyError: message => raceErrors.push(message),
      }, {header:document.querySelector('#header')});
      document.querySelector('#fixture').append(raceView.settings);
    }''')
    expect(summary).to_have_text(locale['routerWatchdog.summaryOff'])
    summary.focus()
    summary.press('Enter')
    action.focus()
    action.press('Enter')
    assert page.evaluate('raceEnsureCalls') == 1
    expect(action).to_have_attribute('aria-busy', 'true')
    page.evaluate('''() => {raceRead = states.unavailable; raceView.reload();}''')
    expect(summary).to_contain_text(locale['rich.unknown'])
    expect(action).not_to_be_visible()
    expect(action).to_be_disabled()
    page.evaluate('''() => {raceRead = states.fresh; raceView.reload();}''')
    expect(summary).to_have_text(locale['routerWatchdog.summaryFaultsOnly'])
    expect(action).to_be_visible()
    expect(action).to_be_enabled()
    expect(action).not_to_have_attribute('aria-busy', 'true')
    page.evaluate('''() => {raceRead = states.unavailable; raceView.reload();}''')
    expect(summary).to_contain_text(locale['rich.unknown'])
    page.evaluate('finishEnsure(states.standing)')
    expect(action).not_to_have_attribute('aria-busy', 'true')
    expect(summary).to_contain_text(locale['rich.unknown'])
    expect(settings.get_by_role('alert')).to_contain_text('unavailable')
    expect(action).not_to_be_visible()
    expect(action).to_be_disabled()
    assert page.evaluate('raceView.conversationId()') == ''
    assert page.evaluate('raceOpened') == []
    assert page.evaluate('raceErrors') == []
    page.evaluate('''() => {raceRead = states.fresh; raceView.reload();}''')
    expect(summary).to_have_text(locale['routerWatchdog.summaryFaultsOnly'])
    expect(action).to_be_enabled()
    action.focus()
    action.press('Enter')
    assert page.evaluate('raceOpened') == ['fresh-watchdog']
    assert page.evaluate('raceEnsureCalls') == 1
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    page.evaluate('raceView.destroy()')
