"""Chat-side Overleaf live preview: compile → render → SyncTeX scroll/pulse.

Pins the TS owner (frontend/src/features/overleaf/live-preview.ts) against a
fake API/pdf.js bridge under jsdom: an agent edit round posts one compile,
the panel renders every page, the SyncTeX rect scrolls into view with a
pulsing highlight, an edit burst coalesces to the newest candidate, and a
rejected compile surfaces an error toast instead of corrupting the panel.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'frontend/src/features/overleaf/live-preview.ts'
ESBUILD = ROOT / 'scripts' / 'vite_test_bundle.mjs'


def _node() -> str:
    executable = shutil.which('node')
    if not executable:
        pytest.skip('node not available')
    return executable


def _has_jsdom() -> bool:
    return subprocess.run(
        [_node(), '-e', "require('jsdom')"], cwd=ROOT,
        capture_output=True,
    ).returncode == 0


@pytest.mark.skipif(not ESBUILD.is_file() or not _has_jsdom(),
                    reason='jsdom + vite test bundler not installed')
def test_live_preview_compiles_renders_scrolls_and_coalesces(tmp_path):
    built = tmp_path / 'live-preview.js'
    compiled = subprocess.run(
        [str(ESBUILD), str(MODULE), '--bundle', '--format=cjs',
         '--platform=node', f'--outfile={built}'],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert compiled.returncode == 0, compiled.stderr

    harness = textwrap.dedent(
        """
        const { JSDOM } = require('jsdom');
        const dom = new JSDOM('<!doctype html><body></body>');
        global.window = dom.window;
        global.document = dom.window.document;
        for (const name of ['HTMLElement', 'AbortController']) {
          global[name] = dom.window[name];
        }
        const rafQueue = [];
        global.requestAnimationFrame = (cb) => { rafQueue.push(cb); };
        const flushRaf = () => {
          const queued = rafQueue.splice(0);
          queued.forEach((cb) => cb(0));
        };
        const scrollCalls = [];
        dom.window.HTMLCanvasElement.prototype.getContext = () => null;
        dom.window.HTMLElement.prototype.scrollTo = function (opts) {
          scrollCalls.push(opts);
        };

        const pid = 'a'.repeat(24);
        const cand = (id, needle) => ({
          toolCallId: id, toolName: 'mcp__overleaf__edit_file',
          projectId: pid, file: 'main.tex', needle,
        });

        const previewCalls = [];
        const latexCalls = [];
        const toasts = [];
        let previewImpl;
        let latexImpl;
        const fakeApi = {
          overleaf: {
            preview: (body) => {
              previewCalls.push(body);
              return previewImpl(body);
            },
          },
          latex: {
            preview: (body) => {
              latexCalls.push(body);
              return latexImpl(body);
            },
          },
        };
        const renderedPages = [];
        const fakeDoc = {
          numPages: 2,
          getPage: async (n) => ({
            getViewport: ({ scale }) => ({ width: 595 * scale, height: 842 * scale }),
            render: () => {
              renderedPages.push(n);
              return { promise: Promise.resolve() };
            },
          }),
          destroy: () => {},
        };
        const deps = {
          api: () => fakeApi,
          loadPdfJs: async () => ({
            getDocument: (url) => ({ promise: Promise.resolve(fakeDoc) }),
          }),
          showToast: (message, kind) => toasts.push({ message, kind }),
          translate: (key) => key,
        };

        const owner = require(BUILT_PATH);
        const preview = owner.createOverleafLivePreview(deps);
        const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
        const results = {};
        const check = (name, value) => { results[name] = !!value; };

        (async () => {
          /* Happy path: one edit round → compile → render → scroll/pulse. */
          previewImpl = async () => ({
            ok: true,
            pdf_url: '/api/v1/overleaf/preview/' + pid + '/pdf',
            page: 2,
            rects: [{ page: 2, h: 72, v: 200, width: 300, height: 18 }],
          });
          preview.observeRounds([cand('call-1', 'needle-one')]);
          for (let i = 0; i < 12; i += 1) await tick();
          flushRaf();

          check('preview_post_carries_project_file_needle',
            previewCalls.length === 1
              && previewCalls[0].project_id === pid
              && previewCalls[0].file === 'main.tex'
              && previewCalls[0].needle === 'needle-one');
          const panel = document.getElementById('overleafLivePanel');
          check('panel_opens', !!panel
            && panel.classList.contains('olp-panel-open'));
          const pages = panel.querySelectorAll('.olp-page');
          check('all_pages_rendered_in_order',
            pages.length === 2
              && pages[0].dataset.page === '1'
              && pages[1].dataset.page === '2'
              && renderedPages.join(',') === '1,2');
          const mark = pages[1].querySelector('.olp-highlight');
          check('synctex_rect_pulsed_on_target_page',
            !!mark && mark.classList.contains('olp-highlight-on'));
          check('panel_scrolled_to_rect',
            scrollCalls.length === 1
              && typeof scrollCalls[0].top === 'number'
              && scrollCalls[0].behavior === 'smooth');

          /* Burst: two edits land while a compile is in flight; only the
           * newest is compiled next (trailing-latest, no stale queue). */
          let releaseFirst;
          previewImpl = (body) => {
            if (previewCalls.length === 2) {
              return new Promise((resolve) => { releaseFirst = resolve; });
            }
            return Promise.resolve({
              ok: true, pdf_url: '/pdf?v=2', page: null, rects: [],
            });
          };
          preview.observeRounds([cand('call-2', 'needle-two')]);
          await tick();
          preview.observeRounds([cand('call-3', 'needle-three-stale')]);
          preview.observeRounds([cand('call-4', 'needle-four-latest')]);
          releaseFirst({ ok: true, pdf_url: '/pdf?v=1', page: null, rects: [] });
          for (let i = 0; i < 12; i += 1) await tick();
          check('burst_coalesces_to_latest_candidate',
            previewCalls.length === 3
              && previewCalls[2].needle === 'needle-four-latest');

          /* Rejected compile: toast + failure status, panel stays usable. */
          previewImpl = async () => ({ ok: false, error: 'compile blew up' });
          preview.observeRounds([cand('call-5', 'needle-five')]);
          for (let i = 0; i < 12; i += 1) await tick();
          check('rejected_compile_toasts_error',
            toasts.length === 1
              && toasts[0].kind === 'error'
              && toasts[0].message.includes('overleaf.compileFailed'));
          check('status_shows_failure_not_busy',
            panel.querySelector('.olp-status').textContent
              === 'overleaf.compileFailed');

          /* Local .tex edit (the no-Overleaf default): posts {path, needle}
           * to the latex namespace and labels the panel with the path. */
          latexImpl = async () => ({
            ok: true,
            pdf_url: '/api/v1/latex/preview/' + 'b'.repeat(16) + '/pdf',
            page: 1,
            rects: [{ page: 1, h: 100, v: 150, width: 200, height: 12 }],
          });
          preview.observeRounds([{
            toolCallId: 'call-local', toolName: 'write_file',
            source: 'local', path: 'paper/main.tex',
            needle: 'local-marker',
          }]);
          for (let i = 0; i < 12; i += 1) await tick();
          check('local_post_carries_path_and_needle',
            latexCalls.length === 1
              && latexCalls[0].path === 'paper/main.tex'
              && latexCalls[0].needle === 'local-marker'
              && latexCalls[0].project_id === undefined);
          check('local_panel_label_shows_path',
            panel.querySelector('.olp-file').textContent === 'paper/main.tex');

          console.log(JSON.stringify(results));
        })().catch((error) => {
          console.error(error && error.stack || error);
          process.exit(1);
        });
        """
    ).replace('BUILT_PATH', json.dumps(str(built)))

    run = subprocess.run(
        [_node(), '-e', harness], cwd=ROOT, capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stderr
    results = json.loads(run.stdout.strip().splitlines()[-1])
    failed = [name for name, ok in results.items() if not ok]
    assert not failed, f'failed pins: {failed}'
    assert len(results) == 10, f'expected 10 pins, got {sorted(results)}'


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
