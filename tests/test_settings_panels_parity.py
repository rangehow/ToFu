"""Closed-system guard for the settings-panel HTML-fragment injection.

Panels were decoupled out of ``index.html`` into ``static/settings_panels/<tab>.html``
fragments, spliced back at render time by ``lib/settings_panels.inject_panels``
(see the settings decoupling batch, 2026-07-18). This is the SAME class of trap
the JS-bundler allowlist guards (CLAUDE.md §3.2.1): a marker with no fragment,
or a fragment with no marker, or a fragment edit that doesn't invalidate the
served-HTML cache, silently makes a settings tab vanish or go stale.

These tests assert the whole loop is closed so any of those breaks fails HERE
instead of as a mysteriously-missing settings page in the browser.

Run isolated (project convention): PYTEST_DISABLE_PLUGIN_AUTOLOAD=1.
"""
from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANELS_DIR = os.path.join(PROJECT_ROOT, 'static', 'settings_panels')

# Every settings tab id wired in index.html's <nav class="settings-tabs"> via
# switchSettingsTab('<tab>'). This is the ground-truth set the served page MUST
# contain a `settingsTab_<tab>` panel div for — whether inline or spliced from a
# fragment. Derived from the nav buttons, asserted below against the HTML so a
# new tab can't be added without appearing here.
_EXPECTED_TABS = frozenset({
    'general', 'api', 'preset', 'search', 'translate', 'speech', 'network',
    'feishu', 'oauth', 'mcp', 'skills', 'preferences', 'advanced',
})


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _index_html():
    return _read(os.path.join(PROJECT_ROOT, 'index.html'))


def _assembled_html():
    """index.html with panel fragments spliced in — what the browser receives."""
    from lib.settings_panels import inject_panels
    return inject_panels(_index_html())


# ── The nav's tab set matches our expected set (catches a new/removed tab) ──

def test_nav_tabs_match_expected_set():
    html = _index_html()
    nav_tabs = set(re.findall(r"switchSettingsTab\('([a-z_]+)'\)", html))
    # switchSettingsTab is also called from a few JS files with 'providers' /
    # 'preferences' etc.; here we only scan index.html's onclick handlers.
    assert nav_tabs == _EXPECTED_TABS, (
        'Settings nav tab set drifted from the expected set. '
        f'nav={sorted(nav_tabs)} expected={sorted(_EXPECTED_TABS)}. '
        'If you added/removed a settings tab, update _EXPECTED_TABS.'
    )


# ── THE core guard: every tab's panel exists in the ASSEMBLED HTML ──────────

def test_all_panels_present_in_assembled_html():
    """After splicing fragments, the served HTML must contain a
    ``id="settingsTab_<tab>"`` panel for EVERY expected tab. A migrated panel
    whose fragment went missing (or whose marker was dropped) fails here."""
    html = _assembled_html()
    missing = [
        tab for tab in _EXPECTED_TABS
        if f'id="settingsTab_{tab}"' not in html
    ]
    assert not missing, (
        'Settings panels ABSENT from the assembled (served) HTML — a fragment '
        f'or its marker is missing: {sorted(missing)}'
    )


def test_no_unresolved_markers_in_assembled_html():
    """After injection there must be NO ``<!-- SETTINGS_PANEL:x -->`` marker
    left — an unresolved marker means the fragment file was missing (the
    injector leaves it in place and logs an error rather than vanish it)."""
    from lib.settings_panels import find_markers
    leftover = find_markers(_assembled_html())
    assert not leftover, (
        'Unresolved SETTINGS_PANEL markers after injection (fragment files '
        f'missing on disk): {sorted(set(leftover))}'
    )


# ── Marker ↔ fragment bidirectional parity ──────────────────────────────────

def test_every_marker_has_a_fragment_file():
    from lib.settings_panels import find_markers, fragment_path
    orphan_markers = [
        tab for tab in set(find_markers(_index_html()))
        if not os.path.exists(fragment_path(tab))
    ]
    assert not orphan_markers, (
        'index.html has SETTINGS_PANEL markers with NO fragment file on disk '
        f'(marker → silent empty panel): {sorted(orphan_markers)}'
    )


def test_every_fragment_file_has_a_marker():
    from lib.settings_panels import find_markers, list_fragment_tabs
    markers = set(find_markers(_index_html()))
    orphan_fragments = sorted(list_fragment_tabs() - markers)
    assert not orphan_fragments, (
        'Fragment files under static/settings_panels/ with NO marker in '
        f'index.html (dead fragment, never served): {orphan_fragments}'
    )


def test_fragment_defines_its_own_panel():
    """A fragment for tab X must actually contain the ``settingsTab_X`` panel
    div — guards a copy-paste that ships the wrong tab's markup."""
    from lib.settings_panels import list_fragment_tabs, fragment_path
    for tab in list_fragment_tabs():
        frag = _read(fragment_path(tab))
        assert f'id="settingsTab_{tab}"' in frag, (
            f'Fragment {tab}.html does not define id="settingsTab_{tab}" '
            '(wrong tab markup?).'
        )


# ── Cache-key safety: fragment edits must invalidate the served-HTML cache ──

def test_panels_signature_changes_when_a_fragment_changes(tmp_path, monkeypatch):
    """A fragment edit MUST change panels_signature() — else index_page's HTML
    cache would serve stale markup after a panel edit (the silent-no-op trap
    this whole mechanism must avoid)."""
    import lib.settings_panels as sp

    d = tmp_path / 'settings_panels'
    d.mkdir()
    (d / 'translate.html').write_text('<div id="settingsTab_translate"></div>')
    monkeypatch.setattr(sp, 'PANELS_DIR', str(d))

    sig1 = sp.panels_signature()
    assert sig1, 'signature should be non-empty when a fragment exists'

    # Rewrite with different content + bump mtime so (mtime,size) both move.
    frag = d / 'translate.html'
    frag.write_text('<div id="settingsTab_translate"><p>changed and longer</p></div>')
    os.utime(str(frag), (10**9, 10**9))  # deterministic distinct mtime
    sig2 = sp.panels_signature()
    assert sig2 != sig1, (
        'panels_signature() did NOT change after a fragment edit — index_page '
        'would serve stale HTML.'
    )


def test_index_cache_key_includes_panels_signature():
    """Structural proof that routes.common threads panels_signature() into the
    served-HTML cache decision (not just computes it and forgets it)."""
    import inspect
    from routes import common
    src = inspect.getsource(common.index_page)
    assert '_settings_panels_signature()' in src, (
        'index_page no longer calls _settings_panels_signature() — fragment '
        'edits would not invalidate the HTML cache.'
    )
    assert "_bundled_index_cache['panels']" in src, (
        'index_page cache-hit check no longer compares the panels signature — '
        'a fragment edit would serve stale cached HTML.'
    )


# ── ALL panels decoupled: the settings modal region is markers-only ────────

def test_all_panels_decoupled_not_inline():
    """The whole-batch invariant: index.html must NOT inline ANY settings panel
    body — every tab is a `<!-- SETTINGS_PANEL:x -->` marker + a fragment file.
    A future edit that re-inlines a panel (or forgets its marker) fails here.

    Note: sibling MODALS (mcpAddOverlay, skillsFilesOverlay, …) legitimately
    remain inline — they are not `settingsTab_*` panels, so this only asserts
    the panel DIVs themselves moved out."""
    html = _index_html()
    still_inline = re.findall(r'id="settingsTab_([a-z_]+)"', html)
    assert not still_inline, (
        'index.html still inlines these settings panels (should be markers + '
        f'fragments): {sorted(still_inline)}'
    )
    # Every expected tab has its marker present.
    missing_markers = [
        tab for tab in _EXPECTED_TABS
        if f'<!-- SETTINGS_PANEL:{tab} -->' not in html
    ]
    assert not missing_markers, (
        f'index.html missing SETTINGS_PANEL markers for: {sorted(missing_markers)}'
    )


def test_translate_fragment_keeps_its_mt_markup():
    """Spot-check one fragment's content survived extraction intact (the pilot,
    with the most distinctive markup)."""
    frag = _read(os.path.join(PANELS_DIR, 'translate.html'))
    assert 'id="mtCardNiutrans"' in frag and 'id="settingMtEnabled"' in frag, (
        'translate.html fragment is missing its expected MT provider markup.'
    )
