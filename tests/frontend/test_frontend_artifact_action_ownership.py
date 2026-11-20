"""Offline ordinary Source/Pin click ownership across panel navigation."""
import pytest

from tests._jsdom import run_harness
from tests.frontend.test_frontend_artifact_version_race import version_panel_bundle
__all__ = ['version_panel_bundle']

pytestmark = pytest.mark.unit

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
function deferred() {
  let resolve;
  const promise = new Promise(ok => { resolve = ok; });
  return { promise, resolve };
}
const stage = __STAGE__, route = __ROUTE__;
const held = deferred();
const calls = { meta: [], raw: [], versions: [], pin: [] };
let holdMeta = false, holdRaw = stage === 'source-raw', holdPin = false;
const meta = (id, pinned = false) => ({ id, title: 'Synthetic ' + id,
  format: 'text', version: 2, pinned });
const { window, document, check, report } = setup({
  root: process.argv[3], html: '<!DOCTYPE html><body></body>', targets: [process.argv[2]],
  globals: {
    disposeRichResponse: () => {}, escapeHtml: String,
    Api: { artifacts: {
      meta: id => { calls.meta.push(id); return holdMeta ? held.promise : Promise.resolve(id === 'error' ? null : meta(id)); },
      contentText: id => { calls.raw.push(id); return holdRaw ? held.promise : Promise.resolve('CURRENT_' + id); },
      versions: async id => { calls.versions.push(id); return { versions: [
        { id: id + '-v1', version: 1 }, { id, version: 2 }] }; },
      pin: (id, pinned) => { calls.pin.push([id, pinned]); return holdPin ? held.promise : Promise.resolve(meta(id, pinned)); },
      library: async () => ({ artifacts: [] }),
    } },
  },
});
const flush = () => new Promise(setImmediate);
const panel = () => document.getElementById('artifact-panel');
const body = () => document.querySelector('[data-art-body]');
const snapshot = () => panel().outerHTML;
(async () => {
  const opening = window.Artifacts.open('A'); await flush();
  if (stage !== 'source-raw') await opening;
  const controls = [...document.querySelectorAll('[data-art-action]')];
  holdMeta = stage.endsWith('meta'); holdPin = stage === 'pin-response';
  const action = stage.startsWith('source') ? 'toggle-source' : 'pin';
  document.querySelector('[data-art-action="' + action + '"]').click(); await flush();
  check('click_reads_exact_selected_id', calls.meta.join(',') === 'A,A');
  check('only_original_mutation_can_start', JSON.stringify(calls.pin) ===
    JSON.stringify(stage === 'pin-response' ? [['A', true]] : []));
  holdMeta = false; holdRaw = false; holdPin = false;
  if (route === 'B' || route === 'ABA') await window.Artifacts.open('B');
  if (route === 'ABA') await window.Artifacts.open('A');
  if (route === 'error') await window.Artifacts.open('error');
  if (route === 'close') window.Artifacts.closePanel();
  if (route === 'library') await window.Artifacts.showLibrary();
  const before = snapshot();
  const beforeCalls = JSON.stringify(calls);
  held.resolve(stage === 'source-raw' ? 'CURRENT_A' : meta('A', stage === 'pin-response'));
  await opening; await flush();
  if (route === 'none') {
    check('normal_selected_action_finishes', action === 'pin'
      ? document.querySelector('[data-art-action="pin"]').getAttribute('aria-pressed') === 'true'
      : body().dataset.sourceView === '1' && body().querySelector('pre').textContent === 'CURRENT_A');
    check('normal_exact_request_counts', calls.meta.join(',') === 'A,A'
      && calls.raw.join(',') === (stage === 'source-raw' ? 'A,A' : 'A')
      && calls.versions.join(',') === (action === 'pin' ? 'A,A' : 'A')
      && JSON.stringify(calls.pin) === JSON.stringify(action === 'pin' ? [['A', true]] : []));
    const selector = document.querySelector('[data-art-version]');
    check('normal_header_selector_controls_stable', document.querySelector('[data-art-title]').textContent === 'Synthetic A'
      && !selector.hidden && selector.value === 'A'
      && [...selector.options].map(o => o.value).join(',') === 'A-v1,A'
      && controls.every(c => c.isConnected));
  } else {
    check('retired_action_cannot_repaint_current_panel', snapshot() === before);
    check('retired_action_issues_no_followup_reads_or_mutations', JSON.stringify(calls) === beforeCalls);
    check('navigation_reads_exact_ids_without_cache_shortcut',
      calls.meta.join(',') === (route === 'ABA' ? 'A,A,B,A' : route === 'B' ? 'A,A,B' : route === 'error' ? 'A,A,error' : 'A,A')
      && calls.raw.join(',') === (stage === 'source-raw' ? 'A,A' : 'A')
        + (route === 'ABA' ? ',B,A' : route === 'B' ? ',B' : '')
      && calls.versions.join(',') === (route === 'ABA' ? 'A,B,A' : route === 'B' ? 'A,B' : 'A'));
  }
  check('only_current_body_retained', route === 'close' || route === 'library' || route === 'error'
    ? window.Artifacts._content.size === 0
    : window.Artifacts._content.size === 1 && window.Artifacts._content.get(route === 'B' ? 'B' : 'A') === 'CURRENT_' + (route === 'B' ? 'B' : 'A'));
  window.Artifacts.closePanel(); report();
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize('stage', ['source-meta', 'source-raw', 'pin-meta', 'pin-response'])
@pytest.mark.parametrize('route', ['none', 'B', 'ABA', 'close', 'library', 'error'])
def test_click_belongs_to_selected_navigation(version_panel_bundle, stage, route):
    run_harness(target_js=version_panel_bundle,
                body_js=_BODY.replace('__STAGE__', repr(stage)).replace('__ROUTE__', repr(route)),
                expect_pass=6, label=f'artifact action {stage} {route}')
