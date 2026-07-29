"""Tofu wordmark parity — the sidebar (18px) and the welcome screen (42px) must
render the SAME wordmark, and the welcome brand area's 落款印 must survive.

WHY THIS EXISTS (found on the shipped CSS, then proven with headless-Chrome pixels)
----------------------------------------------------------------------------------
`tofu` is the DEFAULT theme (index.html:33 falls back to it), so both surfaces are
what every user sees on first paint. They had drifted into TWO wordmarks:

  | | sidebar `.sidebar-brand-o` | welcome `.tofu-brand-o1` |
  |---|---|---|
  | computed color | `rgba(0, 0, 0, 0)` (transparent) | `rgb(169, 101, 54)` |
  | the `o` glyph  | hidden; a CSS `::after` gradient block drew it | a real letter |
  | letter-spacing | `-0.01em` | `-0.03em` |

The welcome screen had been moved to owner's 2026-07-28 方案 A (real letter `o`,
because the concrete cube mascot already sits right above it — a second flat
block in the wordmark is duplicate elements fighting), but the SIDEBAR was never
updated. At 18px the pale block read as a rendering glitch, not a letter.

Root cause is not "someone forgot a value": the two surfaces each carried their
OWN full copy of font-family/weight/letter-spacing/color, so nothing tied them
together and drift was invisible. The fix is a single shared rule
`[data-theme="tofu"] .sidebar-brand, [data-theme="tofu"] .tofu-brand` (+ its
`>span` child rule) that BOTH consume; each surface's own block may then only
set its font-SIZE and its own hover choreography.

WHAT THIS GUARDS
----------------
1. PARITY — resolved letterform properties are EQUAL on both surfaces, computed
   through the real cascade (specificity + source order), not grepped as text.
   This is the invariant that makes a future one-sided edit fail loudly.
2. 方案 A — the `o` is a real, visible letter on BOTH surfaces (not a
   transparent glyph with a pseudo-element block).
3. 落款印 — the welcome `豆腐` is the clay stamp, and its font-size uses a
   `clamp()` FLOOR. The floor is not decoration: at the mobile 20px title a
   pure `.30em` resolves to ~6px and the two Han glyphs smudge together
   (measured, static/icons/_gen/wordmark-preview/a3.png).

Each fix carries an on-disk NEUTER that restores styles.css byte-identical.
"""

from __future__ import annotations

import os
import re

import pytest

# Reuse the project's proven CSS specificity engine + rule iterator.
from tests.test_memory_modal_specificity import (  # noqa: E402
    _Elem,
    _iter_rules,
    _resolve,
)

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')


def _css() -> str:
    with open(CSS, encoding='utf-8') as f:
        return f.read()


def _strip_media_blocks(css_text: str) -> str:
    """Drop every @media block (brace-matched), leaving the desktop cascade.

    Both wordmarks are compared at their DESKTOP resolution; the mobile ladder
    legitimately differs (font-size only) and is asserted separately.
    """
    out = []
    i = 0
    n = len(css_text)
    while i < n:
        m = re.compile(r'@media[^{]*\{').search(css_text, i)
        if not m:
            out.append(css_text[i:])
            break
        out.append(css_text[i:m.start()])
        depth = 0
        j = css_text.find('{', m.start())
        while j < n:
            if css_text[j] == '{':
                depth += 1
            elif css_text[j] == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        i = j + 1
    return ''.join(out)


def _desktop(css_text: str) -> str:
    """Comment-free desktop cascade, with child combinators normalized.

    Two transforms, both required:

    * comments are stripped — the shared engine splits on braces, so a comment
      containing literal braces would corrupt rule parsing;
    * `A>B` is rewritten to `A B`. The shared `_selector_matches` splits
      selectors on WHITESPACE only, so it reads `.tofu-brand>span` as one
      unparseable compound and silently NEVER matches it — a test built on that
      would pass by resolving `None` on both sides instead of comparing real
      values (that is exactly what happened while writing this suite). The
      rewrite is sound here: `_specificity` already collapses `[>+~]` to spaces,
      so specificity is unchanged, and the flat ancestor model makes descendant
      matching equivalent for these one-level-nested spans. Only `>` is
      normalized — turning the SIBLING combinators `+`/`~` into descendants
      would be wrong, so they are left alone.

    The shared engine is deliberately NOT edited: sibling suites depend on it.
    """
    text = re.sub(r'/\*.*?\*/', '', _strip_media_blocks(css_text), flags=re.DOTALL)
    return re.sub(r'\s*>\s*', ' ', text)


# The four letter spans of each surface, as they appear in index.html.
#   sidebar : <h1><span class="sidebar-brand"><span class="sidebar-brand-o">o
#   welcome : <h2 class="tofu-brand"><span class="tofu-brand-o1">o
def _sidebar_letter(cls: str) -> _Elem:
    return _Elem('span', {cls}, theme='tofu',
                 ancestors=[{'sidebar-brand'}, {'sidebar-header'}, {'sidebar'}])


def _welcome_letter(cls: str) -> _Elem:
    return _Elem('span', {cls}, theme='tofu',
                 ancestors=[{'tofu-brand'}, {'welcome'}])


# Wordmark container elements (font-family / letter-spacing live here).
_SIDEBAR_WM = _Elem('span', {'sidebar-brand'}, theme='tofu',
                    ancestors=[{'sidebar-header'}, {'sidebar'}])
_WELCOME_WM = _Elem('h2', {'tofu-brand'}, theme='tofu', ancestors=[{'welcome'}])

# The `o` of each surface — the letter that had diverged.
_SIDEBAR_O = _sidebar_letter('sidebar-brand-o')
_WELCOME_O = _welcome_letter('tofu-brand-o1')

# Properties that define the LETTERFORM. font-size is deliberately excluded:
# 18px vs 42px is the legitimate difference between the two surfaces.
_LETTERFORM_PROPS = ('font-family', 'font-weight', 'letter-spacing')


# ─────────────────────────── 1. parity ───────────────────────────

def test_wordmark_letterform_is_identical_on_both_surfaces():
    """Every letterform property resolves to the SAME value for the sidebar and
    the welcome wordmark. This is the anti-drift invariant."""
    css = _desktop(_css())
    for prop in _LETTERFORM_PROPS:
        side = _resolve(css, _SIDEBAR_WM, prop)
        welc = _resolve(css, _WELCOME_WM, prop)
        assert side is not None, f'sidebar wordmark has no {prop} at all'
        assert side == welc, (
            f'wordmark {prop} DIVERGED: sidebar={side!r} welcome={welc!r}. '
            f'Both surfaces must consume the shared '
            f'`[data-theme="tofu"] .sidebar-brand, .tofu-brand` rule; a surface '
            f'may only override its own font-size.')


def test_wordmark_letter_color_is_identical_on_both_surfaces():
    """The letters themselves resolve to the same ink on both surfaces — this is
    the exact property that had diverged (sidebar `o` was transparent)."""
    css = _desktop(_css())
    for side_cls, welc_cls in (('sidebar-brand-t', 'tofu-brand-t'),
                               ('sidebar-brand-o', 'tofu-brand-o1'),
                               ('sidebar-brand-f', 'tofu-brand-f'),
                               ('sidebar-brand-u', 'tofu-brand-u')):
        for prop in ('color', '-webkit-text-fill-color'):
            side = _resolve(css, _sidebar_letter(side_cls), prop)
            welc = _resolve(css, _welcome_letter(welc_cls), prop)
            assert side == welc, (
                f'{prop} diverged for {side_cls} vs {welc_cls}: '
                f'{side!r} vs {welc!r}')


def test_nc_reintroducing_a_sidebar_only_letterspacing_breaks_parity():
    """NEUTER: give the sidebar its own letter-spacing again (exactly the drift
    that shipped) → parity must fail. Restores styles.css byte-identical."""
    original = _css()
    anchor = ('[data-theme="tofu"] .sidebar-header h1{'
              'font-family:var(--sans-body);font-size:18px;font-weight:800}')
    assert original.count(anchor) == 1, (
        f'NC anchor not unique/found: count={original.count(anchor)}')

    css = _desktop(original)
    assert (_resolve(css, _SIDEBAR_WM, 'letter-spacing')
            == _resolve(css, _WELCOME_WM, 'letter-spacing')), 'baseline not at parity'

    drifted = anchor + '\n[data-theme="tofu"] .sidebar-brand{letter-spacing:-0.01em}'
    try:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original.replace(anchor, drifted, 1))
        css2 = _desktop(_css())
        side = _resolve(css2, _SIDEBAR_WM, 'letter-spacing')
        welc = _resolve(css2, _WELCOME_WM, 'letter-spacing')
        assert side != welc, (
            'NC did not bite: a sidebar-only letter-spacing:-0.01em still '
            f'resolved equal to the welcome value ({side!r}) — the parity test '
            'is not actually reading the sidebar wordmark.')
    finally:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original)
    assert _css() == original, 'CSS not restored byte-identical'


# ─────────────────────────── 2. 方案 A: the `o` is a real letter ───────────────────────────

def test_the_o_is_a_visible_letter_on_both_surfaces():
    """owner 2026-07-28 方案 A: no flat block standing in for the `o`. The glyph
    must be painted (not transparent) on BOTH surfaces."""
    css = _desktop(_css())
    for name, el in (('sidebar', _SIDEBAR_O), ('welcome', _WELCOME_O)):
        for prop in ('color', '-webkit-text-fill-color'):
            val = (_resolve(css, el, prop) or '').strip().lower()
            assert val not in ('transparent', 'rgba(0,0,0,0)', ''), (
                f'{name} `o` resolves {prop}={val!r} — the glyph is invisible, '
                f'which means a pseudo-element block is standing in for it. '
                f'方案 A requires a real, selectable letter.')


def test_shared_span_rule_outranks_a_bare_letter_class_override():
    """The shared `>span` rule must WIN against a bare per-letter class rule.

    Measured while neuter-testing this suite: an injected
    `[data-theme="tofu"] .sidebar-brand-o{color:transparent}` (0,2,0) did NOT
    turn the `o` transparent, because the shared `[data-theme="tofu"]
    .sidebar-brand>span` (0,2,1) carries an extra TYPE selector and outranks it.
    That margin is load-bearing — it is what makes a stray one-letter override
    unable to silently re-open the drift — so it is pinned here rather than left
    as an accident of how the selector happens to be written.
    """
    from tests.test_memory_modal_specificity import _specificity
    shared = _specificity('[data-theme="tofu"] .sidebar-brand>span')
    bare_letter = _specificity('[data-theme="tofu"] .sidebar-brand-o')
    assert shared > bare_letter, (
        f'shared span rule {shared} no longer outranks a bare letter-class rule '
        f'{bare_letter}; a single-letter override could re-introduce the drift.')


def test_no_pseudo_block_paints_the_o_on_either_surface():
    """The retired gradient-block `o` must not come back: any `::after` on a
    wordmark letter resolves to content:none."""
    css = _desktop(_css())
    offenders = []
    for sel, _idx, decls in _iter_rules(css):
        s = sel.replace(' ', '')
        if '::after' not in s:
            continue
        if not any(k in s for k in ('.sidebar-brand-', '.tofu-brand-',
                                    '.sidebar-brand>span', '.tofu-brand>span')):
            continue
        content = decls.get('content', '').strip().lower()
        if content and content != 'none':
            offenders.append((sel, content))
    assert not offenders, (
        f'a pseudo-element is painting a wordmark letter again: {offenders}. '
        f'方案 A retired the flat block `o`.')


# ─────────────────────────── 3. 落款印 (the clay stamp) ───────────────────────────

_STAMP = _Elem('small', set(), theme='tofu',
               ancestors=[{'tofu-brand'}, {'welcome'}])


def test_welcome_stamp_is_a_clay_seal_not_loose_small_text():
    """`豆腐` is the clay stamp: filled background + cream ink + a tilt."""
    css = _desktop(_css())
    bg = (_resolve(css, _STAMP, 'background') or '').lower()
    assert 'gradient' in bg, (
        f'welcome 豆腐 background resolved {bg!r} — expected the clay gradient '
        f'seal. It has reverted to loose small text.')
    transform = (_resolve(css, _STAMP, 'transform') or '').lower()
    assert 'rotate' in transform, (
        f'welcome 豆腐 transform resolved {transform!r} — the seal tilt is gone.')


def test_welcome_stamp_font_size_keeps_a_readable_floor():
    """The stamp scales with the title (em) but MUST keep a px floor.

    Measured: at the mobile 20px title a pure `.30em` is ~6px and the two Han
    glyphs smudge into one another. A bare `em` here is a real mobile defect,
    so the floor is load-bearing, not styling taste.
    """
    css = _desktop(_css())
    size = (_resolve(css, _STAMP, 'font-size') or '').replace(' ', '').lower()
    assert size.startswith('clamp('), (
        f'welcome 豆腐 font-size resolved {size!r} — expected a clamp() with a '
        f'px floor so it stays readable at the mobile 20px title.')
    floor = re.match(r'clamp\((\d+(?:\.\d+)?)px', size)
    assert floor, f'clamp() floor is not an absolute px value: {size!r}'
    assert float(floor.group(1)) >= 10.0, (
        f'clamp() floor {floor.group(1)}px is below the 10px readability floor '
        f'for two Han glyphs.')


def test_nc_dropping_the_clamp_floor_regresses_mobile_readability():
    """NEUTER: replace the clamp() with the bare `.30em` it protects against →
    the floor assertion must fail. Restores styles.css byte-identical."""
    original = _css()
    anchor = 'font-size:clamp(10.5px,.30em,13px);'
    assert original.count(anchor) == 1, (
        f'NC anchor not unique/found: count={original.count(anchor)}')
    try:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original.replace(anchor, 'font-size:.30em;', 1))
        size = (_resolve(_desktop(_css()), _STAMP, 'font-size') or '').lower()
        assert not size.startswith('clamp('), (
            f'NC setup failed: font-size still resolves {size!r}')
    finally:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original)
    assert _css() == original, 'CSS not restored byte-identical'


def test_nc_blanking_the_ground_shadow_is_detected():
    """NEUTER: restore the old `.welcome-icon::before{content:none}` (which sat
    LATER in source than the new ground shadow and would silently kill it) →
    the mascot loses its ground shadow. Restores byte-identical."""
    original = _css()
    shadow_sel = '[data-theme="tofu"] .welcome-icon::before'
    css = _desktop(original)

    def _shadow_content(text: str) -> str:
        best = None
        for sel, idx, decls in _iter_rules(text):
            if sel.replace(' ', '') != shadow_sel.replace(' ', ''):
                continue
            if 'content' in decls:
                best = decls['content'].strip().lower()
        return best or ''

    assert _shadow_content(css) == '""', (
        f'baseline: ground shadow content resolved {_shadow_content(css)!r}, '
        f'expected \'""\' — the mascot has no ground shadow.')
    try:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original + '\n' + shadow_sel + '{content:none}\n')
        assert _shadow_content(_desktop(_css())) == 'none', (
            'NC did not bite: a later content:none did not win, so this test '
            'cannot detect the shadow being blanked.')
    finally:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original)
    assert _css() == original, 'CSS not restored byte-identical'


# ─────────────────────────── 4. markup contract ───────────────────────────

def test_both_surfaces_ship_the_four_letter_spans():
    """The shared `>span` rule only reaches per-letter spans, so the markup must
    keep them (and the welcome `<small>` that carries the seal)."""
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    for cls in ('sidebar-brand-t', 'sidebar-brand-o', 'sidebar-brand-f',
                'sidebar-brand-u', 'tofu-brand-t', 'tofu-brand-o1',
                'tofu-brand-f', 'tofu-brand-u'):
        assert f'class="{cls}"' in html, f'index.html lost the {cls} span'
    assert '<small>豆腐</small>' in html, 'welcome lost the 豆腐 seal element'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
