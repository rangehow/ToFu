"""Normal explicit reader opens retain live reads, content identity and failures."""
import pytest

from tests._jsdom import run_harness
from tests.frontend.test_frontend_artifacts_filter import artifact_bundle
__all__ = ['artifact_bundle']

pytestmark = pytest.mark.unit

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
let allowed = true, rawFailure = false;
let revision = 1, source = '# Benign report\n\nOriginal content.';
const calls = { meta: 0, versions: 0, raw: 0, markdown: 0 };
const { window, document, check, report } = setup({
  root: process.argv[3], targets: [process.argv[2]],
  html: '<!DOCTYPE html><body></body>',
  globals: {
    t: () => '', escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;'),
    disposeRichResponse: () => {},
    renderMarkdown: text => { calls.markdown++; return '<p>' + text + '</p>'; },
    Api: { artifacts: {
      meta: async id => { calls.meta++; return allowed ? {
        id, conv_id: 'fixture-conversation', format: 'markdown', title: 'Benign report',
        version: revision, size_bytes: source.length,
      } : null; },
      versions: async id => { calls.versions++; return { versions: [{ id, version: revision }] }; },
      contentText: async () => { calls.raw++; if (rawFailure) throw new Error('HTTP 403'); return source; },
    } },
  },
});
const body = () => document.querySelector('[data-art-body]');
const counts = () => ({ ...calls });
(async () => {
  const observations = [];
  for (let index = 0; index < 3; index++) {
    const before = counts();
    await window.Artifacts.open('fixture-report');
    observations.push({ delta: Object.fromEntries(Object.keys(calls).map(key => [key, calls[key] - before[key]])),
      output: body().innerHTML });
  }
  check('normal_open_counts', observations.every(row => Object.values(row.delta).every(count => count === 1)));
  check('normal_output_identity', observations.every(row => row.output === observations[0].output));
  check('active_content_identity', window.Artifacts._content.get('fixture-report') === source);
  window.Artifacts.closePanel();
  check('close_releases_content', window.Artifacts._content.size === 0 && body().childElementCount === 0);
  await window.Artifacts.open('fixture-report');
  check('reopen_reads_again', calls.meta === 4 && calls.raw === 4 && body().innerHTML === observations[0].output);

  revision = 2; source = '# Benign report\n\nChanged content.';
  await window.Artifacts.open('fixture-report');
  check('same_id_changed_revision_content', window.Artifacts._meta.get('fixture-report').version === 2
    && window.Artifacts._content.get('fixture-report') === source && body().textContent.includes('Changed content.'));
  allowed = false;
  const beforeDenied = counts();
  await window.Artifacts.open('fixture-report');
  check('denied_meta_stops_raw', calls.meta === beforeDenied.meta + 1 && calls.raw === beforeDenied.raw);
  check('denied_meta_preserves_error', body().textContent.includes('Could not load artifact')
    && body().textContent.includes('artifact meta not found') && !body().textContent.includes('Changed content.'));
  check('denied_open_releases_content', window.Artifacts._content.size === 0);
  allowed = true; rawFailure = true;
  await window.Artifacts.open('fixture-report');
  check('raw_error_preserved', body().textContent.includes('Could not load artifact') && body().textContent.includes('HTTP 403'));
  rawFailure = false;
  await window.Artifacts.open('fixture-report');
  check('normal_retry_recovers', body().textContent.includes('Changed content.'));
  window.Artifacts.closePanel();
  console.log('normal explicit opens: ' + JSON.stringify(observations.map(row => row.delta)));
  report();
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def test_normal_explicit_repeated_opens(artifact_bundle):
    run_harness(target_js=artifact_bundle, body_js=_BODY, expect_pass=11,
                label='normal repeated artifact opens')


_PROJECT_SWITCH_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { performance } = require('node:perf_hooks');
const calls = { list: 0, meta: 0, versions: 0, raw: 0 };
const projects = {
  A: { id: 'synthetic-project-A-chat', projectPath: '/synthetic/A' },
  B: { id: 'synthetic-project-B-chat', projectPath: '/synthetic/B' },
};
let revision = 1, allowed = true, pinned = false;
let chain = [{ id: 'A-report', version: 1 }];
let holdVersions = null, holdRaw = null;
const row = id => ({ id, conv_id: projects[id[0]].id, format: 'markdown',
  title: id + ' report', version: id[0] === 'A' ? revision : 1, pinned });
const source = id => '# ' + id + ' revision ' + (id[0] === 'A' ? revision : 1);
const { window, document, check, report } = setup({
  root: process.argv[3], targets: [process.argv[2]],
  html: '<!DOCTYPE html><body><button id="convFilesBtn"><span data-conv-files-count></span></button></body>',
  globals: {
    activeConvId: null, t: () => '', escapeHtml: String, disposeRichResponse: () => {},
    renderMarkdown: text => '<p>' + text + '</p>',
    ConversationSurfacePresentation: { isMounted: () => true },
    Api: { artifacts: {
      forConv: async id => { calls.list++; return { artifacts: [row(id === projects.A.id ? 'A-report' : 'B-report')] }; },
      meta: async id => { calls.meta++; return allowed ? row(id) : null; },
      versions: async id => { calls.versions++; return holdVersions || { versions: id[0] === 'A' ? chain : [{ id, version: 1 }] }; },
      contentText: async id => { calls.raw++; return holdRaw || source(id); },
    } },
  },
});
global.localStorage = window.localStorage;
const flush = () => new Promise(setImmediate);
const body = () => document.querySelector('[data-art-body]');
const options = () => [...document.querySelectorAll('[data-art-version] option')].map(el => el.value);
async function visit(key) {
  window.activeConvId = projects[key].id;
  window.Artifacts.refreshConversationFiles();
  // Snapshot admission and subsequent activation use the existing catalog-shell owner.
  if (!visited.has(key)) await window.Artifacts.hydrateConversation(projects[key], true);
  else { window.Artifacts.activateConversation(projects[key]); await flush(); }
  visited.add(key);
  document.getElementById('convFilesBtn').click();
  document.querySelector('[data-art-lib-id="' + key + '-report"]').click();
  await flush();
}
const visited = new Set();
(async () => {
  const observations = [];
  for (const key of ['A', 'B', 'A']) {
    const before = { ...calls }, start = performance.now();
    await visit(key);
    observations.push({ project: key, milliseconds: performance.now() - start,
      delta: Object.fromEntries(Object.keys(calls).map(k => [k, calls[k] - before[k]])) });
    check('selected_' + observations.length, body().textContent.includes(key + '-report revision 1'));
  }
  check('one_of_each_per_visit', observations.every(x => Object.values(x.delta).every(n => n === 1)));
  const selected = JSON.stringify(window.Artifacts._meta.get('A-report'));
  chain = [{ id: 'A-report', version: 1 }, { id: 'A-child', version: 2 }];
  await window.Artifacts.open('A-report'); await flush();
  check('same_selected_revision_does_not_prove_chain_complete', selected === JSON.stringify(window.Artifacts._meta.get('A-report'))
    && options().join(',') === 'A-report,A-child');
  pinned = true;
  await window.Artifacts.open('A-report'); await flush();
  check('same_version_metadata_can_change', window.Artifacts._meta.get('A-report').version === 1
    && document.querySelector('[data-art-action="pin"]').getAttribute('aria-pressed') === 'true');
  revision = 2;
  await window.Artifacts.open('A-report'); await flush();
  check('changed_revision_reads_current_body', body().textContent.includes('A-report revision 2'));
  allowed = false;
  const rawBefore = calls.raw;
  await window.Artifacts.open('A-report');
  check('withdrawn_access_does_not_reuse_body', calls.raw === rawBefore && window.Artifacts._content.size === 0
    && body().textContent.includes('Could not load artifact') && !body().textContent.includes('revision 2'));
  allowed = true;
  let releaseVersions, releaseRaw;
  holdVersions = new Promise(resolve => { releaseVersions = resolve; });
  holdRaw = new Promise(resolve => { releaseRaw = resolve; });
  const late = window.Artifacts.open('A-report'); await flush();
  holdVersions = null; holdRaw = null;
  await visit('B');
  releaseVersions({ versions: chain }); releaseRaw('STALE A BODY');
  await late; await flush();
  check('retired_open_cannot_publish_body_or_versions', body().textContent.includes('B-report revision 1')
    && !body().textContent.includes('STALE') && options().length === 0);
  window.Artifacts.closePanel();
  check('close_releases_active_body', window.Artifacts._content.size === 0 && body().childElementCount === 0);
  console.log('synthetic reader project switch (logical API calls, no network; local hook/list/header/body wall ms): '
    + JSON.stringify(observations));
  report();
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def test_project_switch_report_version_freshness(artifact_bundle):
    output = run_harness(target_js=artifact_bundle, body_js=_PROJECT_SWITCH_BODY, expect_pass=10,
                         label='project switch report version freshness')
    print(next(line for line in output.splitlines() if line.startswith('synthetic reader project switch')))


_RETIRED_RAW_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
let resolveOld, rejectOld;
const oldRaw = new Promise((resolve, reject) => { resolveOld = resolve; rejectOld = reject; });
const calls = { meta: [], versions: [], raw: [] };
const { window, document, check, report } = setup({
  root: process.argv[3], targets: [process.argv[2]],
  html: '<!DOCTYPE html><body></body>',
  globals: {
    t: () => '', escapeHtml: String, disposeRichResponse: () => {},
    renderMarkdown: text => '<p>' + text + '</p>',
    Api: { artifacts: {
      meta: async id => { calls.meta.push(id); return { id, title: id, format: 'markdown', version: 1 }; },
      versions: async id => { calls.versions.push(id); return { versions: [{ id, version: 1 }] }; },
      contentText: id => {
        calls.raw.push(id);
        return id === 'A' && calls.raw.length === 1 ? oldRaw : Promise.resolve('CURRENT_' + id);
      },
    } },
  },
});
global.localStorage = window.localStorage;
const flush = () => new Promise(setImmediate);
const body = () => document.querySelector('[data-art-body]');
(async () => {
  const retired = window.Artifacts.open('A'); await flush();
  check('first_A_raw_is_pending', calls.raw.join(',') === 'A' && !window.Artifacts._content.has('A'));
  await window.Artifacts.open('B');
  await window.Artifacts.open('A');
  check('revisit_has_current_body_and_cache', body().textContent === 'CURRENT_A'
    && window.Artifacts._content.get('A') === 'CURRENT_A');
  check('each_navigation_reads_fresh_exact_identity', ['meta', 'versions', 'raw'].every(key => calls[key].join(',') === 'A,B,A'));
  __SETTLE__
  await retired; await flush();
  check('retired_settlement_preserves_current_cache', window.Artifacts._content.size === 1
    && window.Artifacts._content.get('A') === 'CURRENT_A');
  check('retired_settlement_preserves_current_body', body().textContent === 'CURRENT_A');
  document.querySelector('[data-art-action="toggle-source"]').click(); await flush();
  check('source_view_uses_current_content', body().dataset.sourceView === '1'
    && body().querySelector('pre').textContent === 'CURRENT_A');
  check('source_view_reuses_only_active_body', calls.raw.join(',') === 'A,B,A'
    && calls.meta.join(',') === 'A,B,A,A' && calls.versions.join(',') === 'A,B,A');
  window.Artifacts.closePanel();
  check('close_releases_current_content', window.Artifacts._content.size === 0);
  report();
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize('settlement', ['success', 'failure'])
def test_retired_raw_settlement_after_same_artifact_revisit(artifact_bundle, settlement):
    settle = ("resolveOld('RETIRED_A');" if settlement == 'success'
              else "rejectOld(new Error('retired raw failure'));")
    run_harness(target_js=artifact_bundle,
                body_js=_RETIRED_RAW_BODY.replace('__SETTLE__', settle), expect_pass=8,
                label=f'retired raw ABA {settlement}')
