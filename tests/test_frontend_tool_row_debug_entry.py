"""Tool-row debug entry: geometry, single-entry, and model-view coexistence.

The defect this pins (owner-verified against a real screenshot): the debug
entry rendered as a ZERO-HEIGHT block (`.ri-tool-anchor-row{height:0}`) whose
child floated up with `margin-top:-14px`, while `.tc-preview-btn` (model view)
claimed the same right edge with `margin-left:auto`. Two elements each believed
they owned the row's right end, so they PRINTED ON TOP OF EACH OTHER.

Why these assertions are GEOMETRIC and run in a REAL browser: the overlap is a
layout fact. jsdom computes no layout, so a jsdom assertion here would be green
against any CSS whatsoever — including the broken CSS. The suite therefore
drives the real bundle and reads real `getBoundingClientRect()`s.

Covered (each an observable RESULT, not an implementation detail):
  1. The debug entry and the model-view button do NOT intersect, on every tool
     row on screen.
  2. COMPLEMENT — the model-view button still EXISTS and is visible. Without
     this, "delete the debug entry entirely" (or "delete model view") would
     satisfy #1 and the suite would stay green while the product got worse.
  3. ONE debug entry per row, not two (R and S merged into a single control).
  4. The entry is discoverable WITHOUT hover (it used to be `opacity:0`).
  5. Exactly ONE element in the row header owns the right edge via
     `margin-left:auto` — the structural invariant behind #1, so a future row
     type that re-adds a second `auto` fails here rather than by visibly
     overlapping in production.

NEUTER (see test_neuter_*): restore the negative-margin overlay and #1 must go
red. A guard that cannot detect the original defect is not a guard.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = [pytest.mark.visual]

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')

# A synthetic settled turn with tool rounds, injected straight into the
# renderer so the suite does not depend on a live model call. `llmRound` and
# `_taskId` are present because the entry is (correctly) provenance-gated.
_SEED_JS = r"""
(() => {
  window._featureFlags = window._featureFlags || {};
  window._featureFlags.debug_mode = true;
  const rounds = [
    { roundNum: 1, llmRound: 0, toolName: 'run_command', status: 'done',
      toolContent: 'hello from the tool', _taskId: 'task-geom-1',
      toolArgs: { command: 'ls -la' },
      results: [{ title: 'run_command', snippet: 'ok' }] },
    { roundNum: 2, llmRound: 1, toolName: 'read_files', status: 'done',
      toolContent: 'file body', _taskId: 'task-geom-1',
      results: [{ title: 'read_files', snippet: 'ok' }] },
  ];
  const host = document.createElement('div');
  host.id = 'geomHost';
  // Match the production nesting so the real .ptool-* rules apply.
  host.className = 'ptool-panel';
  host.innerHTML = '<div class="ptool-panel-body">' +
    renderToolRoundsHTML(rounds, false, null) + '</div>';
  host.style.position = 'relative';
  document.body.appendChild(host);
  return host.querySelectorAll('[data-prn]').length;
})()
"""

_MEASURE_JS = r"""
(() => {
  const out = [];
  document.querySelectorAll('#geomHost [data-prn]').forEach((slot) => {
    const entries = slot.querySelectorAll('.ri-tool-anchor');
    const views = slot.querySelectorAll('.tc-preview-btn');
    const rect = (el) => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, w: r.width, h: r.height };
    };
    const vis = (el) => {
      const cs = getComputedStyle(el);
      return cs.display !== 'none' && cs.visibility !== 'hidden' &&
             parseFloat(cs.opacity) > 0.01;
    };
    out.push({
      prn: slot.getAttribute('data-prn'),
      entries: [...entries].map((e) => ({
        r: rect(e), visible: vis(e), label: e.textContent.trim(),
      })),
      views: [...views].map((v) => ({ r: rect(v), visible: vis(v) })),
      autoOwners: [...slot.querySelectorAll('.ptool-line > *, .ptool-line')]
        .filter((el) => getComputedStyle(el).marginLeft === 'auto')
        .map((el) => el.className),
    });
  });
  return out;
})()
"""


def _intersects(a, b):
    """True when two rects overlap by more than a hairline (anti-aliasing)."""
    eps = 0.5
    return (a['x'] < b['x'] + b['w'] - eps and b['x'] < a['x'] + a['w'] - eps
            and a['y'] < b['y'] + b['h'] - eps and b['y'] < a['y'] + a['h'] - eps)


@pytest.fixture()
def _rows(page, live_server):
    page.goto(live_server, wait_until='domcontentloaded')
    page.wait_for_function('typeof renderToolRoundsHTML === "function"',
                           timeout=30000)
    n = page.evaluate(_SEED_JS)
    assert n >= 2, f'seed rendered {n} tool slots, expected >= 2'
    return page.evaluate(_MEASURE_JS)


def test_debug_entry_does_not_overlap_model_view(_rows):
    """#1 — the actual reported defect, asserted geometrically."""
    for row in _rows:
        for e in row['entries']:
            for v in row['views']:
                assert not _intersects(e['r'], v['r']), (
                    f"row {row['prn']}: debug entry {e['r']} overlaps the "
                    f"model-view button {v['r']} — they must share one flex "
                    f"flow, each occupying its own space")


def test_model_view_button_survives_and_is_visible(_rows):
    """#2 COMPLEMENT — model view is a DIFFERENT question from the debug
    entry (verbatim bytes returned to the model vs. how the call came to be),
    so it must not be removed or hidden to satisfy the geometry test."""
    seen = 0
    for row in _rows:
        for v in row['views']:
            assert v['r']['w'] > 0 and v['r']['h'] > 0, (
                f"row {row['prn']}: model-view button has zero size")
            assert v['visible'], f"row {row['prn']}: model-view button hidden"
            seen += 1
    assert seen >= 2, f'expected a model-view button per row, saw {seen}'


def test_exactly_one_debug_entry_per_row(_rows):
    """#3 — R and S were merged into ONE control with in-panel tabs."""
    for row in _rows:
        assert len(row['entries']) == 1, (
            f"row {row['prn']}: expected exactly 1 debug entry, found "
            f"{len(row['entries'])} ({[e['label'] for e in row['entries']]})")


def test_debug_entry_is_discoverable_without_hover(_rows):
    """#4 — it used to be `opacity:0` until the row was hovered, which makes
    it a control the user never finds."""
    for row in _rows:
        for e in row['entries']:
            assert e['visible'], (
                f"row {row['prn']}: debug entry is not visible without hover")
            assert e['r']['h'] > 0, (
                f"row {row['prn']}: debug entry has zero height — it is an "
                f"overlay again, not a participant in the row's flow")


def test_single_owner_of_the_row_right_edge(_rows):
    """#5 — the structural invariant behind #1."""
    for row in _rows:
        assert len(row['autoOwners']) <= 1, (
            f"row {row['prn']}: {len(row['autoOwners'])} elements claim the "
            f"right edge with margin-left:auto ({row['autoOwners']}) — "
            f"exactly one wrapper may own it")


def test_neuter_restoring_the_negative_margin_overlay_is_caught(page, live_server):
    """NEUTER: put the ORIGINAL broken CSS back (zero-height row + negative
    top margin + a second margin-left:auto) and assert the geometry test would
    flip red. Without this we cannot tell a working guard apart from one that
    is simply compatible with every possible layout."""
    page.goto(live_server, wait_until='domcontentloaded')
    page.wait_for_function('typeof renderToolRoundsHTML === "function"',
                           timeout=30000)
    page.evaluate(_SEED_JS)
    # Re-introduce the defect at runtime (never touches the shipped file).
    page.add_style_tag(content=(
        '.ptool-row-ctl{margin-left:0 !important}'
        '.ptool-line .tc-preview-btn{margin-left:auto !important}'
        '.ri-tool-anchor{position:absolute !important;right:8px !important;'
        'margin-top:-14px !important}'
    ))
    rows = page.evaluate(_MEASURE_JS)
    overlapped = any(
        _intersects(e['r'], v['r'])
        for row in rows for e in row['entries'] for v in row['views'])
    assert overlapped, (
        'NEUTER did not reproduce the overlap — the geometry assertion is '
        'therefore not load-bearing and must be redesigned')


def test_shipped_css_has_no_negative_margin_overlay():
    """Ratchet on the specific broken construct, anchored to the rule body so
    a reformat of the stylesheet cannot silently retire it."""
    css = open(CSS, encoding='utf-8').read()
    m = re.search(r'\.ri-tool-anchor\{([^}]*)\}', css)
    assert m, '.ri-tool-anchor rule not found — re-anchor this guard'
    body = m.group(1)
    assert 'margin-top:-' not in body, (
        f'.ri-tool-anchor floats over the row again: {body}')
    assert not re.search(r'opacity:\s*0[;,}]', body), (
        f'.ri-tool-anchor is hover-only again (opacity:0): {body}')
    row = re.search(r'\.ri-tool-anchor-row\{([^}]*)\}', css)
    assert row and 'height:0' not in row.group(1), (
        '.ri-tool-anchor-row is a zero-height overlay again')
