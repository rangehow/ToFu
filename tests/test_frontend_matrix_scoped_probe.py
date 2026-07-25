#!/usr/bin/env python3
"""tests/test_frontend_matrix_scoped_probe.py — scoped probe UI + panel fit.

Drives the REAL shipped ``static/js/settings/providers/access_matrix.js``
under node (same harness pattern as tests/test_probe_nonchat_skip.py) and
pins the scoped-probe contract (2026-07-25):

  * RENDER — every key header carries a column probe button dispatching
    ``{key_idxs:[ki]}``; every model row (root AND alias) carries a row
    button dispatching ``{model_ids:[id]}``; a cell without a verdict shows
    a hover probe button; a cell WITH a verdict turns its pip into the
    re-probe trigger (exact (key, id) pair in the onclick).
  * STATE — while a scoped probe runs, the scoped cell/row/column renders
    the spinning 'probing' state and other scope buttons are disabled;
    the scope is cleared when a terminal snapshot is ingested.
  * DISPATCH — ``_runMatrixProbe(provIdx, false, only)`` posts the scope to
    the backend (``body.only``), does NOT set force (a scoped probe is a
    refresh, not a reset), and KEEPS the locally-known verdict cells so the
    grid merges instead of blanking.
  * PANEL FIT — ``_fitMatrixPanelWidth`` toggles .stg-matrix-wide on the
    settings panel iff an open matrix's scroll container overflows.

NEUTER proof: deleting ``if (only) body.only = only;`` from a COPY of the
file flips the dispatch assertions red (the scope buttons would silently
launch full-grid probes).

Also pins the CSS affordances this feature relies on (settings.css):
always-visible matrix scrollbar, scroll-shadow layers, panel-wide rule.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
ACCESS_MATRIX_JS = os.path.join(ROOT, 'static', 'js', 'settings', 'providers', 'access_matrix.js')
SETTINGS_CSS = os.path.join(ROOT, 'static', 'settings.css')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.addEventListener = function () {};           // resize hook registered at eval
global.document = {
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
};
global.t = function (k) { return k; };
global.escapeHtml = function (s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
};
global.showToast = function () {};
global._renderProvidersTab = function () {};
global._stgProviders = [];
global._maskApiKey = function (k) { return k ? '…' + k.slice(-4) : ''; };
global._detectBrand = function () { return ''; };
global._brandSvg = function () { return ''; };

// Api stub: capture probe-start bodies; status polls return 'none'.
var _postedBodies = [];
global.Api = {
  providers: {
    probeCellsStart: function (body) {
      _postedBodies.push(body);
      return { then: function () { return { catch: function () {} }; } };
    },
    probeCellsStatus: function () {
      return { then: function () { return { catch: function () {} }; } };
    },
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL access_matrix.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

_stgProviders[0] = {
  id: 'p',
  api_keys: ['sk-aaa', 'sk-bbb'],
  models: [
    { model_id: 'm1', aliases: ['m1-a'], capabilities: ['text'] },
    { model_id: 'm2', capabilities: ['text'] },
  ],
};
_stgMatrixProbeAttached[0] = true;   // skip the resume-from-disk timer

// ── RENDER: column / row / cell triggers ──────────────────────────────
const matrixHtml = _renderAccessMatrix(0);
check('col_button_present', matrixHtml.indexOf('stg-mx-zap col') >= 0);
check('col_button_dispatches_key_scope',
      matrixHtml.indexOf('_probeMatrixScope(0,{key_idxs:[1]})') >= 0);
check('row_button_root_dispatches_model_scope',
      matrixHtml.indexOf('_probeMatrixScope(0,{model_ids:[&quot;m1&quot;]})') >= 0);
check('row_button_alias_dispatches_alias_scope',
      matrixHtml.indexOf('_probeMatrixScope(0,{model_ids:[&quot;m1-a&quot;]})') >= 0);
check('row_buttons_rendered_per_row',
      (matrixHtml.match(/stg-mx-zap row/g) || []).length === 3);

// Cell without a verdict → hover probe button for the exact pair.
const m1 = _stgProviders[0].models[0];
const cellHtml = _renderMatrixCell(0, 0, 1, m1, 'm1', false);
check('cell_button_present', cellHtml.indexOf('stg-mx-zap cell') >= 0);
check('cell_button_dispatches_exact_pair',
      cellHtml.indexOf('_probeMatrixScope(0,{key_idxs:[1],model_ids:[&quot;m1&quot;]})') >= 0);

// Cell WITH a verdict → the pip itself becomes the re-probe trigger.
_stgMatrixProbe[0] = {
  status: 'done',
  cells: { '0::m1': { key_idx: 0, model_id: 'm1', root_model_id: 'm1',
                      status: 'ok', detail: 'HTTP 200', recommend_disable: false } },
  summary: { ok: 1, disable: 0 }, total: 1, done_count: 1,
};
const pipCell = _renderMatrixCell(0, 0, 0, m1, 'm1', false);
check('pip_is_clickable_reprobe', pipCell.indexOf('stg-mx-probe-pip ok clickable') >= 0);
check('pip_dispatches_exact_pair',
      pipCell.indexOf('_probeMatrixScope(0,{key_idxs:[0],model_ids:[&quot;m1&quot;]})') >= 0);
check('no_separate_cell_button_when_pip', pipCell.indexOf('stg-mx-zap cell') < 0);

// ── STATE: running scope → spinner on the target, others disabled ──────
_stgMatrixProbeScope[0] = { key_idxs: [0], model_ids: ['m1'] };
_stgMatrixProbe[0].status = 'running';
const spinning = _renderMatrixCell(0, 0, 0, m1, 'm1', false);
check('scoped_cell_spins', spinning.indexOf('stg-mx-zap cell probing') >= 0);
check('spinning_cell_hides_stale_pip', spinning.indexOf('probe-pip ok clickable') < 0);
const otherCell = _renderMatrixCell(0, 0, 1, m1, 'm1', false);
check('other_cell_button_disabled_while_running', otherCell.indexOf('disabled') >= 0);
check('scopeCovers_cell_exact', _scopeCovers(0, 'cell', 0, 'm1') === true);
check('scopeCovers_cell_wrong_key', _scopeCovers(0, 'cell', 1, 'm1') === false);
check('scopeCovers_row_vs_cell_scope', _scopeCovers(0, 'row', null, 'm1') === false);

// Terminal ingest clears the scope.
_ingestProbeSnapshot(0, { status: 'done', cells: {}, summary: { ok: 0, disable: 0 } });
check('terminal_ingest_clears_scope', !_stgMatrixProbeScope[0]);

// ── DISPATCH: scoped start posts `only`, keeps local cells, no force ──
_postedBodies.length = 0;
_stgMatrixProbe[0] = {
  status: 'done',
  cells: { '0::m1': { key_idx: 0, model_id: 'm1', root_model_id: 'm1',
                      status: 'ok', detail: 'HTTP 200', recommend_disable: false } },
  summary: { ok: 1, disable: 0 }, total: 1, done_count: 1,
};
_runMatrixProbe(0, false, { key_idxs: [1] });
check('scoped_dispatch_posts_only',
      _postedBodies.length === 1 &&
      JSON.stringify(_postedBodies[0].only) === '{"key_idxs":[1]}');
check('scoped_dispatch_never_forces', _postedBodies[0].force === false);
check('scoped_start_keeps_local_verdicts',
      _stgMatrixProbe[0].cells['0::m1'] && _stgMatrixProbe[0].cells['0::m1'].status === 'ok');
check('scoped_start_records_scope',
      _stgMatrixProbeScope[0] && _stgMatrixProbeScope[0].key_idxs[0] === 1);
check('scoped_start_status_running', _stgMatrixProbe[0].status === 'running');

// Full retest still forces + clears cells (regression pin).
_postedBodies.length = 0;
_stgMatrixProbe[0].status = 'done';
_runMatrixProbe(0, true);
check('full_retest_still_forces',
      _postedBodies.length === 1 && _postedBodies[0].force === true &&
      !_postedBodies[0].only);
check('full_retest_clears_cells',
      Object.keys(_stgMatrixProbe[0].cells).length === 0);

// A second probe click while running is refused (backend is single-task).
_postedBodies.length = 0;
_probeMatrixScope(0, { model_ids: ['m2'] });
check('concurrent_scope_refused', _postedBodies.length === 0);

// ── PANEL FIT: widen only while a matrix overflows ─────────────────────
_stgMatrixProbe[0].status = 'done';
const toggles = [];
const fakePanel = { classList: { toggle: function (cls, on) { toggles.push([cls, on]); } } };
let fakeScrolls = [{ scrollWidth: 900, clientWidth: 400 }];
global.document.querySelector = function (sel) {
  return sel === '.modal.settings-panel' ? fakePanel : null;
};
global.document.querySelectorAll = function (sel) {
  return sel === '.stg-matrix-scroll' ? fakeScrolls : [];
};
_fitMatrixPanelWidth();
check('overflowing_matrix_widens_panel',
      toggles.length === 1 && toggles[0][0] === 'stg-matrix-wide' && toggles[0][1] === true);
fakeScrolls = [{ scrollWidth: 300, clientWidth: 400 }];
_fitMatrixPanelWidth();
check('fitting_matrix_unwidens_panel',
      toggles.length === 2 && toggles[1][1] === false);

console.log(out.join('\n'));
process.exit(0);
"""


def _run_harness(matrix_js: str) -> str:
    harness = os.path.join(HERE, '_matrix_scoped_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, matrix_js],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
class ScopedProbeFrontendTest(unittest.TestCase):

    def test_scoped_probe_render_state_dispatch_panelfit(self):
        output = _run_harness(ACCESS_MATRIX_JS)
        fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
        self.assertEqual(fails, [], 'scoped-probe frontend failures:\n' + output)
        self.assertGreaterEqual(output.count('PASS'), 20,
                                'expected >=20 PASS lines, got:\n' + output)

    def test_neuter_scope_dispatch_is_load_bearing(self):
        """NEUTER: drop `if (only) body.only = only;` from a COPY → the scope
        buttons silently launch full-grid probes → dispatch checks go red."""
        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            src = f.read()
        anchor = '  if (only) body.only = only;\n'
        self.assertIn(anchor, src, 'dispatch anchor drifted — update the neuter')
        neutered = src.replace(anchor, '', 1)
        self.assertNotEqual(neutered, src)

        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, 'access_matrix_neutered.js')
            with open(copy, 'w', encoding='utf-8') as f:
                f.write(neutered)
            output = _run_harness(copy)
        self.assertIn('FAIL scoped_dispatch_posts_only', output,
                      'NEUTER did not bite: scope still posted without the only line.\n'
                      + output)

        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            self.assertEqual(f.read(), src, 'harness mutated the shipped access_matrix.js')


class ScopedProbeCssTest(unittest.TestCase):
    """Static pins for the affordances: visible scrollbar, scroll shadows,
    panel auto-widen, zap placements, model-id ellipsis cap."""

    @classmethod
    def setUpClass(cls):
        with open(SETTINGS_CSS, encoding='utf-8') as f:
            cls.css = f.read()

    def test_always_visible_scrollbar_rules(self):
        self.assertIn('.stg-matrix-scroll::-webkit-scrollbar', self.css)
        self.assertIn('scrollbar-width: thin', self.css)

    def test_scroll_shadow_layers(self):
        self.assertIn('background-attachment: local, local, scroll, scroll', self.css)

    def test_panel_wide_rule_desktop_only(self):
        self.assertIn('.settings-panel.stg-matrix-wide', self.css)
        self.assertIn('@media (min-width: 769px)', self.css)

    def test_zap_placements_and_pip_clickable(self):
        for sel in ('.stg-mx-zap.col', '.stg-mx-zap.row', '.stg-mx-zap.cell',
                    '.stg-mx-zap.probing', '.stg-mx-probe-pip.clickable'):
            self.assertIn(sel, self.css, sel + ' missing from settings.css')

    def test_model_id_ellipsis_cap(self):
        self.assertIn('.stg-mx-mid.alias-id', self.css)


if __name__ == '__main__':
    unittest.main()
