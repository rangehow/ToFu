"""Offline artifact-panel navigation with synthetic metadata and version reads."""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.test_frontend_artifact_version_race import version_panel_bundle as version_panel_bundle

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1280, 390])
@pytest.mark.parametrize('failure', ['null', 'reject'])
def test_selector_clears_during_failed_open_and_recovers(browser, version_panel_bundle, tmp_path, width, failure):
    root = Path(__file__).resolve().parents[2]
    with browser.new_context(offline=True, service_workers='block', viewport={'width': width, 'height': 900}) as context:
        requests = []
        context.route('**/*', lambda route: route.abort())
        context.on('request', lambda request: requests.append(request.url))
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_content('<!doctype html><html lang="en"><body></body></html>')
        for stylesheet in ['00-design-tokens.css', '18-artifacts-knowledge-library.css']:
            page.add_style_tag(path=str(root / 'frontend/src/styles/application' / stylesheet))
        page.evaluate("""() => {
          Object.defineProperty(window, 'localStorage', {value: {getItem: () => null, setItem: () => {}}});
          window.disposeRichResponse = () => {};
          window.calls = {meta: [], versions: [], raw: []};
          let aOpens = 0;
          window.Api = {artifacts: {
            meta: id => {
              calls.meta.push(id);
              if (id === 'B') return new Promise((resolve, reject) => {
                window.resolveMeta = resolve; window.rejectMeta = reject;
              });
              if (id === 'A') aOpens++;
              return Promise.resolve({id, title: 'Synthetic ' + id, format: 'text', version: 2});
            },
            contentText: async id => {calls.raw.push(id); return 'CURRENT_' + id;},
            versions: id => {
              calls.versions.push(id);
              if (aOpens === 2) return new Promise(resolve => {window.resolveVersions = resolve;});
              return Promise.resolve({versions: [{id: 'A-v1', version: 1}, {id: 'A', version: 2}]});
            },
          }};
        }""")
        page.add_script_tag(path=version_panel_bundle)
        page.evaluate("Artifacts.open('A')")
        selector = page.locator('[data-art-version]')
        body = page.locator('[data-art-body]')
        expect(selector).to_be_visible()
        expect(selector).to_have_value('A')
        page.evaluate("() => {window.openB = Artifacts.open('B');}")
        expect(body).to_contain_text('Loading artifact')
        expect(selector).to_be_hidden()
        expect(selector.locator('option')).to_have_count(0)
        page.evaluate("""failure => {
          if (failure === 'null') resolveMeta(null);
          else rejectMeta(new Error('synthetic metadata failure'));
          return openB;
        }""", failure)
        expect(body).to_contain_text('Could not load artifact')
        expect(body).to_contain_text('artifact meta not found' if failure == 'null' else 'synthetic metadata failure')
        expect(selector).to_be_hidden()
        expect(selector.locator('option')).to_have_count(0)
        page.screenshot(path=str(tmp_path / f'failed-open-{width}-{failure}.png'))
        page.evaluate("Artifacts.open('A')")
        expect(body).to_have_text('CURRENT_A')
        expect(selector).to_be_hidden()
        expect(selector.locator('option')).to_have_count(0)
        page.evaluate("resolveVersions({versions: [{id: 'A-v1', version: 1}, {id: 'A', version: 2}, {id: 'A-v3', version: 3}]})")
        expect(selector).to_be_visible()
        expect(selector).to_have_value('A')
        assert selector.locator('option').evaluate_all('(options) => options.map(o => o.value)') == ['A-v1', 'A', 'A-v3']
        assert page.evaluate('calls') == {'meta': ['A', 'B', 'A'], 'versions': ['A', 'A'], 'raw': ['A', 'A']}
        page.locator('[data-art-action="close"]').click()
        expect(page.locator('#artifact-panel')).to_have_attribute('aria-hidden', 'true')
        assert requests == []
        assert errors == []
