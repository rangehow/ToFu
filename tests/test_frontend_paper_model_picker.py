#!/usr/bin/env python3
"""jsdom + static guards for the Reading-Mode model picker's label.

Two defects this pins (both user-reported as "the model name is cut off /
not fully shown" in the Reading-Mode toolbar):

  * The chosen model's short name must SURVIVE ``_applyI18n()``. The markup
    ships ``data-i18n="paper.reportSelectModel"`` on the label span for the
    initial placeholder; ``_applyI18n()`` walks every ``[data-i18n]`` and
    overwrites textContent. It runs on boot AND on every language toggle, so
    leaving the attribute in place silently replaced the model name with
    "Select model" — the one piece of state the button exists to show.
  * A long id is ellipsized by CSS, so the FULL id must stay recoverable via
    the button's tooltip (and the static ``data-i18n-title`` must be dropped
    for the same clobber reason).

Static guard: the CSS max-width must fit the longest real model short-name.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
REPORT_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'report.js')
STYLES_CSS = os.path.join(ROOT, 'static', 'styles.css')
INDEX_HTML = os.path.join(ROOT, 'index.html')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


#: The label span + button markup mirrors index.html (both report and review
#: pickers ship the same data-i18n placeholder pair).
_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(`<!DOCTYPE html><body>
  <div class="paper-report-model-picker">
    <button class="paper-report-model-btn"
            data-i18n-title="paper.reportSelectModelTitle"
            title="Select model for report generation">
      <span id="paperReportModelLabel" data-i18n="paper.reportSelectModel">Select model</span>
    </button>
    <div class="paper-report-model-dropdown" id="paperReportModelDropdown"></div>
  </div>
</body>`, { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;

const T_MAP = {
  'paper.reportSelectModel': 'SELECT_MODEL_PLACEHOLDER',
  'paper.reportSelectModelTitle': 'PICK_A_MODEL_TITLE',
};
win.t = global.t = (k) => T_MAP[k] || k;
global._modelShortName = win._modelShortName = (id) =>
  String(id || '').replace(/^(aws\.|vertex\.)/, '').split('/').pop();

/* The real _applyI18n contract (static/js/i18n.js): walk every [data-i18n]
   and overwrite textContent; same for [data-i18n-title] → title. */
function _applyI18n() {
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    const k = el.getAttribute('data-i18n');
    if (k) el.textContent = t(k);
  });
  document.querySelectorAll('[data-i18n-title]').forEach((el) => {
    const k = el.getAttribute('data-i18n-title');
    if (k) el.title = t(k);
  });
}

/* Minimal view-context stand-in — the shipped _reportView lives in
   paper-reader.js; the picker only reads these three fields. */
let VIEW_MODEL = '';
global._reportView = win._reportView = () => ({
  modelDropdownId: 'paperReportModelDropdown',
  modelLabelId: 'paperReportModelLabel',
  get model() { return VIEW_MODEL; },
  set model(v) { VIEW_MODEL = v; },
});
global._registeredModels = win._registeredModels = [];
global._hiddenModels = win._hiddenModels = new Set();

/* Load ONLY the picker functions out of the real shipped report.js — the file
   is a 2700-line module whose top level touches many other globals. Slicing by
   the function boundary keeps this a test of the REAL code, not a copy. */
const src = fs.readFileSync(path.join(ROOT, 'static/js/paper/report.js'), 'utf8');
const start = src.indexOf('function _selectPaperReportModel(');
if (start < 0) { console.log('FAIL harness_cannot_find_function'); process.exit(0); }
const end = src.indexOf('\nfunction _togglePaperReportModelDropdown', start);
eval(src.slice(start, end > 0 ? end : undefined));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const LONG_ID = 'yuju-claude-opus-5-evaDaily';
const label = document.getElementById('paperReportModelLabel');
const btn = label.closest('.paper-report-model-btn');

// Baseline: the placeholder is i18n-driven before any choice is made.
_applyI18n();
check('placeholder_is_i18n_driven',
  label.textContent === 'SELECT_MODEL_PLACEHOLDER');

// Choose a model → the label shows its short name.
_selectPaperReportModel(LONG_ID);
check('label_shows_model', label.textContent === LONG_ID);
check('full_id_in_button_tooltip', btn.title === LONG_ID);

// ★ The regression: a language toggle (or boot) re-runs _applyI18n().
_applyI18n();
check('model_name_survives_i18n_reapply', label.textContent === LONG_ID);
check('tooltip_survives_i18n_reapply', btn.title === LONG_ID);
// Run it a few more times — idempotent, never drifts back to the placeholder.
_applyI18n(); _applyI18n();
check('model_name_survives_repeated_i18n', label.textContent === LONG_ID);

// Empty model (no chat models available) keeps the button usable.
_selectPaperReportModel('');
check('empty_model_falls_back_to_placeholder',
  label.textContent === 'SELECT_MODEL_PLACEHOLDER');

console.log(out.join('\n'));
process.exit(0);
"""


def _run_harness(src_override: str | None = None) -> str:
    harness = os.path.join(HERE, '_model_label_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(src_override if src_override is not None else _HARNESS)
    try:
        proc = subprocess.run(['node', harness, '-', ROOT],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_model_label_survives_language_toggle():
    out = _run_harness()
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'model-picker label failures:\n' + out
    assert out.count('PASS') >= 7, f'expected >=7 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_data_i18n_removal_is_loadbearing():
    """NEUTER: drop the ``removeAttribute('data-i18n')`` line from a COPY of
    report.js → the next _applyI18n() overwrites the model name with the
    placeholder and the survival probe flips FAIL. Proves the attribute
    removal (not the textContent write) is what makes the choice stick."""
    src = open(REPORT_JS, encoding='utf-8').read()
    marker = "      label.removeAttribute('data-i18n');"
    assert marker in src, 'data-i18n removal marker not found — test is stale'
    broken = src.replace(marker, '      /* NEUTERED */', 1)
    assert broken != src

    tmp_js = os.path.join(HERE, '_report_no_i18n_removal.js')
    with open(tmp_js, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp_js], capture_output=True,
                             text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        patched_harness = _HARNESS.replace(
            "path.join(ROOT, 'static/js/paper/report.js')",
            repr(tmp_js).replace("'", '"'))
        out = _run_harness(patched_harness)
        assert 'FAIL model_name_survives_i18n_reapply' in out, \
            'removing the data-i18n cleanup did NOT flip the probe:\n' + out
    finally:
        try:
            os.remove(tmp_js)
        except OSError:
            pass
    assert open(REPORT_JS, encoding='utf-8').read() == src, 'shipped file modified!'


# ═══ Static guards (no node required) ═══

def test_model_btn_width_fits_longest_model_name():
    """The button must be wide enough for the longest real model short-name.

    Measured from the shipped provider config: ids reach 30 characters
    (e.g. ``gemini-3.1-flash-image-preview``). At the button's 11px semibold
    font ~30 chars needs roughly 200px of text box plus ~46px of chrome
    (two SVG icons + gaps + padding), so the cap must clear ~250px. The old
    180px cut ``yuju-claude-opus-5-e…`` off at 21 chars, which is the
    reported symptom.
    """
    css = open(STYLES_CSS, encoding='utf-8').read()
    m = re.search(r'\.paper-report-model-btn\{[^}]*?max-width:(\d+)px', css)
    assert m, '.paper-report-model-btn max-width rule not found'
    assert int(m.group(1)) >= 250, (
        f'model button max-width is {m.group(1)}px — too narrow for a 30-char '
        'model id; long names get ellipsized in the toolbar')


def test_model_btn_label_span_still_ellipsizes():
    """Widening is not a licence to overflow: the span must still clip so a
    pathological id can never push the toolbar sideways."""
    css = open(STYLES_CSS, encoding='utf-8').read()
    m = re.search(r'\.paper-report-model-btn span\{([^}]*)\}', css)
    assert m, '.paper-report-model-btn span rule not found'
    body = m.group(1)
    for prop in ('overflow:hidden', 'text-overflow:ellipsis', 'white-space:nowrap'):
        assert prop in body, f'{prop} missing from the label span rule'


def test_both_pickers_ship_the_same_placeholder_markup():
    """Report AND review pickers must both carry the i18n placeholder — the
    JS clobber fix is shared, so a divergence here would silently mean one
    tab's label is not covered by it."""
    html = open(INDEX_HTML, encoding='utf-8').read()
    for label_id in ('paperReportModelLabel', 'paperReviewModelLabel'):
        m = re.search(r'<span id="' + label_id + r'"([^>]*)>', html)
        assert m, f'{label_id} span not found in index.html'
        assert 'data-i18n="paper.reportSelectModel"' in m.group(1), (
            f'{label_id} lost its data-i18n placeholder — the initial '
            '"Select model" text would no longer localize')


def test_static_js_syntax():
    if not shutil.which('node'):
        pytest.skip('node not installed')
    proc = subprocess.run(['node', '--check', REPORT_JS],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f'report.js syntax: {proc.stderr}'


if __name__ == '__main__':
    import sys
    test_model_btn_width_fits_longest_model_name()
    test_model_btn_label_span_still_ellipsizes()
    test_both_pickers_ship_the_same_placeholder_markup()
    if _node_deps_available():
        test_static_js_syntax()
        test_model_label_survives_language_toggle()
        test_NEUTER_data_i18n_removal_is_loadbearing()
    else:
        print('SKIP jsdom cases — node + jsdom not available')
    print('ALL PASSED')
    sys.exit(0)
