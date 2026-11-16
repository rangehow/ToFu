"""Capability toggles that are on together stay distinct rows with one on-state.

The Extras toggles are independent, so several are often on at once. Flush rows
fused their highlights into one slab (Tofu's stacked spines drew a curly
bracket) and a stale ID rule painted Local Control a foreign hue.
"""
import re

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.visual]

# One synchronous task: the app's own toggle sync cannot interleave, and with
# motion disabled the computed style is already the settled end state.
_TURN_ALL_ON = r'''() => {
  const menu = document.querySelector('#submenuExtras .submenu-dropdown');
  const rows = [...menu.querySelectorAll('.submenu-item[id$="Toggle"]')]
    .filter(row => row.getClientRects().length);
  rows.forEach(row => row.classList.add('active'));
  return rows.map(row => {
    const box = row.getBoundingClientRect(), style = getComputedStyle(row);
    return {id: row.id, top: box.top, bottom: box.bottom, shadow: style.boxShadow,
            paint: `${style.backgroundColor} ${style.backgroundImage}`};
  });
}'''


def _touching(rows):
    return [(upper['id'], lower['id']) for upper, lower in zip(rows, rows[1:])
            if lower['top'] - upper['bottom'] < 1]


@pytest.mark.parametrize('theme', ['tofu', 'light', 'dark'])
def test_concurrent_capability_toggles_render_as_distinct_rows(page, assert_no_js_errors, theme):
    from tests.visual.test_visual_surfaces import _invoke_action, _wait_ready

    page.set_viewport_size({'width': 1280, 'height': 900})
    _wait_ready(page)
    page.evaluate('theme => { document.documentElement.dataset.theme = theme; }', theme)
    page.add_style_tag(content='#submenuExtras, #submenuExtras * {transition:none !important; animation:none !important}')
    _invoke_action(page, 'toggleSubmenu', 'submenuExtras')
    rows = page.evaluate(_TURN_ALL_ON)

    assert len(rows) >= 3, rows
    assert not _touching(rows), rows
    assert len({row['paint'] for row in rows}) == 1, rows
    assert not [row for row in rows if 'inset' in row['shadow']], rows
    if theme == 'tofu':
        selected = page.evaluate(
            "getComputedStyle(document.querySelector('#agentModeMenu .flow-menu-item.selected')).boxShadow")
        assert 'inset' in selected, 'the spine still marks the single-select choice'

    # NEUTER: forcing flush rows must trip the same detector.
    page.add_style_tag(content='#submenuExtras .submenu-item{margin:0 !important}')
    assert _touching(page.evaluate(_TURN_ALL_ON))


_CHECK_OPACITY = r'''theme => {
  document.documentElement.dataset.theme = theme;
  return getComputedStyle(
    document.querySelector('#localControlToggle .submenu-item-check')).opacity;
}'''


def test_unavailable_local_control_row_is_current_and_checks_only_when_on(page, assert_no_js_errors):
    """"Unavailable" describes now, and an off row never shows a check.

    The modal's poll was the only reachability refresh, so a row probed before
    the extension connected kept "unavailable" after it connected; the
    unavailable dimming rule also matched off rows and revealed their check.
    """
    from tests.visual.test_visual_surfaces import _invoke_action, _wait_ready

    world = {'browser': False}

    def status(route):
        if '/browser/status' in route.request.url:
            data = {'connected': world['browser'], 'clients': []}
        else:
            data = {'connected': False, 'setup_state': 'pair', 'nodes': []}
        route.fulfill(json={'data': data, 'meta': {'requestId': 'lc-reach'}})

    page.route('**/api/v4/browser/status', status)
    page.route('**/api/v4/desktop/status*', status)
    page.set_viewport_size({'width': 1280, 'height': 900})
    _wait_ready(page)
    page.add_style_tag(content='#submenuExtras, #submenuExtras * {transition:none !important; animation:none !important}')
    row = page.locator('#localControlToggle')
    unavailable = re.compile(r'\btool-unavailable\b')

    _invoke_action(page, 'openLocalControlModal')
    expect(row).to_have_class(unavailable)
    _invoke_action(page, 'closeLocalControlModal')
    _invoke_action(page, 'toggleSubmenu', 'submenuExtras')
    expect(row).to_have_class(unavailable)
    expect(row).not_to_have_class(re.compile(r'\bactive\b'))
    for theme in ('tofu', 'light', 'dark'):
        assert page.evaluate(_CHECK_OPACITY, theme) == '0', theme
    row.evaluate("el => el.classList.add('active')")
    for theme in ('tofu', 'light', 'dark'):
        assert 0 < float(page.evaluate(_CHECK_OPACITY, theme)) < 1, theme
    row.evaluate("el => el.classList.remove('active')")

    # NEUTER: the selector that also matched off rows must trip the same probe.
    old_rule = page.add_style_tag(
        content='.submenu-item.tool-unavailable .submenu-item-check{opacity:.42}')
    assert page.evaluate(_CHECK_OPACITY, 'tofu') != '0'
    old_rule.evaluate('el => el.remove()')

    world['browser'] = True
    _invoke_action(page, 'toggleSubmenu', 'submenuExtras')
    _invoke_action(page, 'toggleSubmenu', 'submenuExtras')
    expect(row).not_to_have_class(unavailable)
