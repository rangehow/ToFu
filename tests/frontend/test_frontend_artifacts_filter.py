"""Regression harness for the artifacts-library filter empty-state.

WHY
---
``_filterLibrary`` in ``static/js/artifacts.js`` hides non-matching rows with
``display:none``. When a query matched NOTHING it hid every row and showed no
fallback — the library looked blank/broken (same class as the conv-list
collapse bug: a control left with zero visible items and no message). The fix
appends a ``.artifact-lib-noresult`` line when ``visible === 0``.

This test renders the real library markup, runs the real ``_filterLibrary``,
and asserts the no-match line appears for a zero-match query and disappears
again when the query clears. Skips cleanly without node + jsdom.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def artifact_bundle():
    root = Path(__file__).resolve().parents[2]
    source = root/'frontend/src/runtime/sections/artifacts.js'
    # Shipping imports are relative to composed runtime/app-runtime.js. Compile
    # the actual adapter/typed presenter graph rather than eval ESM as classic JS.
    contents = 'const runtimeScope = window;\n' + source.read_text()
    for owner in ('artifact-delivery', 'artifact-review'):
        contents = contents.replace(f"'../rich-response/{owner}'",
                                    repr(str(root/f'frontend/src/rich-response/{owner}.ts')))
    with compiled_typescript(source, contents=contents) as bundle:
        yield bundle

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="artifact-panel"></div></body>',
  targets: [process.argv[2]],
});

// Build the library markup the way _renderLibrary does (search box + rows).
// _filterLibrary only needs .artifact-lib-list + .artifact-lib-row[data-art-search].
const panel = document.getElementById('artifact-panel');
panel.innerHTML =
  '<div class="artifact-lib">' +
    '<div class="artifact-lib-list">' +
      '<button class="artifact-lib-row" data-art-search="alpha report pdf"></button>' +
      '<button class="artifact-lib-row" data-art-search="beta notes html"></button>' +
    '</div>' +
  '</div>';

// _filterLibrary is module-private (inside an IIFE) in the real file; the
// frontend exposes it via window when bundled. Resolve whichever is defined.
const filt = (window.Artifacts && window.Artifacts._filterLibrary) || null;
check('filter_fn_exists', typeof filt === 'function');

function visibleRows() {
  return [...panel.querySelectorAll('.artifact-lib-row')]
    .filter(r => r.style.display !== 'none').length;
}
function noResultShown() {
  const n = panel.querySelector('.artifact-lib-noresult');
  return !!(n && n.style.display !== 'none');
}

if (typeof filt === 'function') {
  // 1. zero-match query → all rows hidden + no-result line shown
  filt('zzz-nothing-matches');
  check('zeromatch_hides_all_rows', visibleRows() === 0);
  check('zeromatch_shows_noresult', noResultShown());

  // 2. matching query → row visible, no-result hidden
  filt('alpha');
  check('match_shows_row', visibleRows() === 1);
  check('match_hides_noresult', !noResultShown());

  // 3. cleared query → all rows visible again, no-result hidden
  filt('');
  check('cleared_shows_all', visibleRows() === 2);
  check('cleared_hides_noresult', !noResultShown());
}

report();
"""

_SNAPSHOT_HINT_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
let listCalls = 0;
let nextListPromise = null;
let nextRows = [];
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><button id="convFilesBtn" style="display:none">' +
        '<span data-conv-files-count>0</span></button></body>',
  targets: [process.argv[2]],
  globals: {
    activeConvId: null,
    disposeRichResponse: () => {},
    Api: { artifacts: { forConv: async () => {
      listCalls += 1;
      if (nextListPromise) return nextListPromise;
      return { artifacts: nextRows };
    } } },
    ConversationSurfacePresentation: { isMounted: () => true },
  },
});
const button = document.getElementById('convFilesBtn');
const shown = () => button.style.display !== 'none';
const count = () => button.querySelector('[data-conv-files-count]').textContent;

(async () => {
  window.activeConvId = 'empty-conv';
  await window.Artifacts.hydrateConversation({ id: 'empty-conv' }, false);
  check('negative_hint_skips_list', listCalls === 0);
  check('negative_hint_hides_entry', !shown());

  window.activeConvId = 'files-conv';
  const filesConversation = { id: 'files-conv' };
  nextRows = [
    { id: 'a1', title: 'report.md', format: 'markdown', version: 1, created_at: 1,
      source_ref: { path: 'out/report.md' } },
    { id: 'a2', title: 'report.md', format: 'markdown', version: 2, created_at: 5,
      source_ref: { path: 'out/report.md' } },
    { id: 'b1', title: 'notes', format: 'markdown', created_at: 3 },
  ];
  await window.Artifacts.hydrateConversation(filesConversation);
  check('missing_hint_keeps_legacy_fetch', listCalls === 1);
  check('one_entry_for_conversation', shown() && count() === '2');

  button.click();
  const rows = [...document.querySelectorAll('#artifact-panel .artifact-lib-row')]
    .map((row) => row.getAttribute('data-art-lib-id'));
  check('list_keeps_latest_version_newest_first', JSON.stringify(rows) === '["a2","b1"]');
  check('entry_marks_open_list', button.classList.contains('active'));

  window.activeConvId = 'other-conv';
  window.Artifacts.refreshConversationFiles();
  check('switch_hides_entry', !shown());
  check('switch_closes_list',
        document.getElementById('artifact-panel').classList.contains('artifact-panel-hidden'));
  window.activeConvId = 'files-conv';
  window.Artifacts.refreshConversationFiles();
  check('return_restores_entry', shown() && count() === '2');

  let releaseStaleList;
  nextListPromise = new Promise((resolve) => { releaseStaleList = resolve; });
  const staleList = window.Artifacts.hydrateConversation(filesConversation, true);
  await Promise.resolve();
  await window.Artifacts.hydrateConversation(filesConversation, false);
  releaseStaleList({ artifacts: nextRows });
  await staleList;
  check('race_issued_one_list', listCalls === 2);
  check('newer_negative_hint_invalidates_stale_list', !shown());
  report();
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""


def test_artifacts_filter_shows_empty_state(artifact_bundle):
    run_harness(
        target_js=artifact_bundle,
        body_js=_BODY,
        min_pass=7,
        label='artifacts filter',
    )


def test_conversation_related_files_list(artifact_bundle):
    run_harness(
        target_js=artifact_bundle,
        body_js=_SNAPSHOT_HINT_BODY,
        expect_pass=11,
        label='artifact related files',
    )
