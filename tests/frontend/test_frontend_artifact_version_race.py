"""Synthetic ordinary artifact-panel version request settlement regressions."""
from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def version_panel_bundle():
    root = Path(__file__).resolve().parents[2]
    source = root / 'frontend/src/runtime/sections/artifacts.js'
    contents = 'const runtimeScope = window;\n' + source.read_text()
    for owner in ('artifact-delivery', 'artifact-review'):
        contents = contents.replace(f"'../rich-response/{owner}'",
                                    repr(str(root / f'frontend/src/rich-response/{owner}.ts')))
    with compiled_typescript(source, contents=contents) as bundle:
        yield bundle


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}
const old = deferred(), latest = deferred();
const calls = [];
const route = __ROUTE__;
const failure = __FAILURE__;
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[2]],
  globals: {
    disposeRichResponse: () => {},
    Api: { artifacts: {
      meta: async id => ({ id, title: 'Synthetic ' + id, format: 'text', version: 2 }),
      contentText: async id => 'Synthetic body ' + id,
      versions: id => {
        calls.push(id);
        if (calls.length === 1) return old.promise;
        if (route === 'ABA' && calls.length === 2) return Promise.resolve({ versions: [] });
        return latest.promise;
      },
    } },
  },
});
const settle = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const chain = id => ({ versions: [{ id: id + '-v1', version: 1 }, { id, version: 2 }] });
const selector = () => document.querySelector('[data-art-version]');
const state = () => ({ hidden: selector().hidden, value: selector().value,
  options: [...selector().options].map(o => [o.value, o.textContent]) });
(async () => {
  await window.Artifacts.open('A');
  if (route === 'ABA') await window.Artifacts.open('B');
  const current = route === 'AB' ? 'B' : 'A';
  await window.Artifacts.open(current);
  check('normal_request_sequence', JSON.stringify(calls) === JSON.stringify(route === 'ABA' ? ['A', 'B', 'A'] : ['A', current]));
  latest.resolve(chain(current));
  await settle();
  const expected = { hidden: false, value: current,
    options: [[current + '-v1', 'v1'], [current, 'v2']] };
  check('latest_success_renders_selector', JSON.stringify(state()) === JSON.stringify(expected));
  if (failure === 'stale-reject') old.reject(new Error('synthetic old versions failure'));
  else old.resolve(chain('old'));
  await settle();
  check('stale_settlement_preserves_latest_selector', JSON.stringify(state()) === JSON.stringify(expected));
  check('latest_title_and_body_remain', document.querySelector('[data-art-title]').textContent === 'Synthetic ' + current &&
    document.querySelector('[data-art-body]').textContent === 'Synthetic body ' + current);
  check('no_extra_versions_requests', calls.length === (route === 'ABA' ? 3 : 2));
  window.Artifacts.closePanel();
  report();
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize('route', ['AB', 'ABA'])
@pytest.mark.parametrize('failure', ['stale-reject', 'stale-success'])
def test_old_version_settlement_preserves_latest_selector(version_panel_bundle, route, failure):
    run_harness(target_js=version_panel_bundle,
                body_js=_BODY.replace('__ROUTE__', repr(route)).replace('__FAILURE__', repr(failure)),
                expect_pass=5, label=f'artifact versions {route} {failure}')


_CURRENT_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
let request = 0;
const { window, document, check, report } = setup({
  root: process.argv[3], html: '<!DOCTYPE html><body></body>', targets: [process.argv[2]],
  globals: {
    disposeRichResponse: () => {},
    Api: { artifacts: {
      meta: async id => ({ id, title: 'Synthetic A', format: 'text' }),
      contentText: async () => 'Synthetic body A',
      versions: async () => {
        if (++request === 1) return { versions: [{ id: 'A-v1', version: 1 }, { id: 'A', version: 2 }] };
        __RESULT__
      },
    } },
  },
});
(async () => {
  await window.Artifacts.open('A');
  const sel = document.querySelector('[data-art-version]');
  check('seed_selector_visible', !sel.hidden && sel.options.length === 2);
  await window.Artifacts.open('A');
  check('current_failure_hides_and_clears_selector', sel.hidden && sel.options.length === 0);
  check('current_failure_keeps_document', document.querySelector('[data-art-body]').textContent === 'Synthetic body A' &&
    document.querySelector('[data-art-title]').textContent === 'Synthetic A' && !document.querySelector('.artifact-error'));
  check('only_existing_versions_requests', request === 2);
  window.Artifacts.closePanel();
  report();
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize('result', ["throw new Error('synthetic current versions failure');", 'return null;', 'return { versions: [] };'])
def test_current_versions_failure_contract(version_panel_bundle, result):
    run_harness(target_js=version_panel_bundle, body_js=_CURRENT_BODY.replace('__RESULT__', result),
                expect_pass=4, label='artifact current versions failure')


_NAVIGATION_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
let releaseMeta, rejectMeta, releaseVersions;
const pendingMeta = new Promise((resolve, reject) => { releaseMeta = resolve; rejectMeta = reject; });
const pendingVersions = new Promise(resolve => { releaseVersions = resolve; });
const calls = { meta: [], versions: [], raw: [] };
let aOpens = 0;
const chain = ids => ({ versions: ids.map((id, index) => ({ id, version: index + 1 })) });
const { window, document, check, report } = setup({
  root: process.argv[3], html: '<!DOCTYPE html><body></body>', targets: [process.argv[2]],
  globals: {
    disposeRichResponse: () => {}, escapeHtml: String,
    Api: { artifacts: {
      meta: id => {
        calls.meta.push(id);
        if (id === 'B') return pendingMeta;
        if (id === 'A') aOpens++;
        return Promise.resolve({ id, title: 'Synthetic ' + id, format: 'text', version: 2 });
      },
      contentText: async id => { calls.raw.push(id); return 'CURRENT_' + id; },
      versions: id => {
        calls.versions.push(id);
        return id === 'A' && aOpens === 2 ? pendingVersions : Promise.resolve(chain(['A-v1', 'A']));
      },
    } },
  },
});
global.localStorage = window.localStorage;
const flush = () => new Promise(setImmediate);
const selector = () => document.querySelector('[data-art-version]');
const body = () => document.querySelector('[data-art-body]');
const empty = () => selector().hidden && selector().options.length === 0 && selector().value === '';
const controls = () => [...document.querySelectorAll('[data-art-action]')];
(async () => {
  await window.Artifacts.open('A');
  check('A_has_current_visible_chain', !selector().hidden && selector().value === 'A'
    && [...selector().options].map(o => o.value).join(',') === 'A-v1,A');
  const originalControls = controls();
  const openingB = window.Artifacts.open('B');
  check('new_navigation_clears_selector_before_metadata', empty());
  check('pending_metadata_keeps_loading_body', body().textContent.includes('Loading artifact'));
  await flush();
  check('pending_open_has_no_extra_navigation', calls.meta.join(',') === 'A,B');
  __SETTLE_META__
  await openingB;
  check('metadata_failure_keeps_selector_empty', empty());
  check('metadata_failure_preserves_error', body().textContent.includes('Could not load artifact')
    && body().textContent.includes('__ERROR__') && !body().textContent.includes('CURRENT_A'));
  check('failed_open_has_no_versions_or_raw_read', calls.meta.join(',') === 'A,B'
    && calls.versions.join(',') === 'A' && calls.raw.join(',') === 'A');
  check('failed_open_releases_previous_source_body', window.Artifacts._content.size === 0);
  await window.Artifacts.open('A');
  check('revisit_body_does_not_wait_for_versions', body().textContent === 'CURRENT_A');
  check('current_versions_pending_keeps_selector_empty', empty());
  releaseVersions(chain(['A-v1', 'A', 'A-v3'])); await flush();
  check('revisit_renders_fresh_growing_chain', !selector().hidden && selector().value === 'A'
    && [...selector().options].map(o => o.value).join(',') === 'A-v1,A,A-v3');
  check('fresh_navigation_request_cardinality', calls.meta.join(',') === 'A,B,A'
    && calls.versions.join(',') === 'A,A' && calls.raw.join(',') === 'A,A');
  check('same_navigation_controls_remain', controls().length === originalControls.length
    && controls().every((control, index) => control === originalControls[index]));
  document.querySelector('[data-art-action="toggle-source"]').click(); await flush();
  check('source_view_keeps_current_cache_and_queries', body().querySelector('pre').textContent === 'CURRENT_A'
    && window.Artifacts._content.get('A') === 'CURRENT_A' && calls.meta.join(',') === 'A,B,A,A'
    && calls.versions.join(',') === 'A,A' && calls.raw.join(',') === 'A,A');
  selector().value = 'A-v1'; selector().dispatchEvent(new window.Event('change', { bubbles: true })); await flush();
  check('existing_version_control_navigates_normally', body().textContent === 'CURRENT_A-v1'
    && selector().value === 'A-v1' && calls.meta.join(',') === 'A,B,A,A,A-v1'
    && calls.versions.join(',') === 'A,A,A-v1' && calls.raw.join(',') === 'A,A,A-v1');
  window.Artifacts.closePanel();
  report();
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize(('settle_meta', 'error'), [
    ('releaseMeta(null);', 'artifact meta not found'),
    ("rejectMeta(new Error('synthetic metadata failure'));", 'synthetic metadata failure'),
])
def test_navigation_clears_previous_versions_before_metadata(version_panel_bundle, settle_meta, error):
    run_harness(target_js=version_panel_bundle,
                body_js=_NAVIGATION_BODY.replace('__SETTLE_META__', settle_meta).replace('__ERROR__', error),
                expect_pass=15, label='artifact selector navigation lifecycle')
