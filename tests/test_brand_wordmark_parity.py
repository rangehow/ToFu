"""Tofu wordmark parity — the sidebar and the welcome screen must render the SAME
wordmark IN EVERY THEME, and the welcome brand area's 落款印 must survive.

WHY THIS EXISTS (found on the shipped CSS, then proven with headless-Chrome pixels)
----------------------------------------------------------------------------------
Round 1 (commit 4c3ad19a) found the two surfaces had drifted apart under the
`tofu` theme: the sidebar `o` was a transparent glyph with an `::after` gradient
block standing in for it, while the welcome screen used a real letter.

Round 2 (this suite's current shape) is the owner bounce: that first fix was
prefixed `[data-theme="tofu"]`, so under **dark / light** the two surfaces were
still two wordmarks — measured sidebar 700 / +0.6px / purple gradient vs welcome
600 / -0.72px / gold gradient. dark rendered a PURPLE sidebar Tofu beside a GOLD
welcome Tofu.

The root cause was deeper than colour, and it is why the drift stayed invisible:
`.tofu-brand` (0,1,0) declared `font-weight:700` + `letter-spacing:0.08em`, but
the generic `.welcome h2` (0,1,1) outranks it and crushed them to 600 / -0.03em.
The sidebar's identical intent DID apply. One declaration, opposite results, and
source that looked like both surfaces asked for 700/0.08em — dead code
masquerading as configuration.

So the letterform source now lives in the THEME-AGNOSTIC base layer with
context-carrying selectors (`.sidebar-header h1 .sidebar-brand` = 0,2,2 /
`.welcome h2.tofu-brand` = 0,2,1) that strictly outrank `.welcome h2`; themes may
override COLOUR only.

WHAT THIS GUARDS
----------------
1. PARITY, PER THEME — resolved letterform properties are EQUAL on both surfaces
   for EVERY theme in `_THEMES`. Parametrized deliberately: a tofu-only assertion
   is exactly what let the dark/light divergence ship.
2. 方案 A — the `o` is a real, visible letter on both surfaces (no
   pseudo-element block standing in for it).
3. 落款印 — the welcome `豆腐` is the clay stamp and its font-size keeps a
   `clamp()` px FLOOR. Not decoration: at the mobile 20px title a pure `.30em`
   resolves to ~6px and the two Han glyphs smudge together (measured,
   static/icons/_gen/wordmark-preview/a3.png).

NEUTERS ARE IN-MEMORY ONLY. An earlier revision applied them by writing the real
static/styles.css and restoring it in a `finally:`. On this shared worktree that
is unsafe in two directions, both observed: a concurrent sibling read the file
mid-window and the suite reported a bogus failure (`''.count` — it had read an
empty file), and worse, a sibling committing inside the restore window would ship
NEUTERED CSS. Every assertion here only needs `_resolve(css_text, ...)`, so the
neuters mutate a STRING and the working tree is never touched.
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
    _specificity,
)

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')

# Every theme the app can be in. `tofu` is the default (index.html falls back to
# it), but dark/light are one click away in Settings and shipped a purple-vs-gold
# split for months precisely because nothing asserted them.
_THEMES = ('tofu', 'dark', 'light')


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


@pytest.fixture(scope='module')
def css_text() -> str:
    """The shipped CSS, read ONCE. Nothing in this suite writes it back."""
    return _css()


# ── element models, parametrized by theme ────────────────────────────────────
# The four letter spans of each surface, as they appear in index.html:
#   sidebar : <h1><span class="sidebar-brand"><span class="sidebar-brand-o">o
#   welcome : <h2 class="tofu-brand"><span class="tofu-brand-o1">o
def _sidebar_letter(cls: str, theme: str) -> _Elem:
    return _Elem('span', {cls}, theme=theme,
                 ancestors=[{'sidebar-brand'}, {'sidebar-header'}, {'sidebar'}])


def _welcome_letter(cls: str, theme: str) -> _Elem:
    return _Elem('span', {cls}, theme=theme,
                 ancestors=[{'tofu-brand'}, {'welcome'}])


def _sidebar_wm(theme: str) -> _Elem:
    return _Elem('span', {'sidebar-brand'}, theme=theme,
                 ancestors=[{'sidebar-header'}, {'sidebar'}])


def _welcome_wm(theme: str) -> _Elem:
    return _Elem('h2', {'tofu-brand'}, theme=theme, ancestors=[{'welcome'}])


def _stamp(theme: str) -> _Elem:
    return _Elem('small', set(), theme=theme,
                 ancestors=[{'tofu-brand'}, {'welcome'}])


# Properties that define the LETTERFORM. font-size is deliberately excluded:
# 15/18px vs 24/42px is the legitimate difference between the two surfaces.
_LETTERFORM_PROPS = ('font-family', 'font-weight', 'letter-spacing')

# The per-letter class pairs (sidebar class, welcome class).
_LETTER_PAIRS = (('sidebar-brand-t', 'tofu-brand-t'),
                 ('sidebar-brand-o', 'tofu-brand-o1'),
                 ('sidebar-brand-f', 'tofu-brand-f'),
                 ('sidebar-brand-u', 'tofu-brand-u'))


# ─────────────────────────── 1. parity, per theme ───────────────────────────

@pytest.mark.parametrize('theme', _THEMES)
def test_wordmark_letterform_is_identical_on_both_surfaces(css_text, theme):
    """Every letterform property resolves to the SAME value on both surfaces, in
    EVERY theme. This is the anti-drift invariant."""
    css = _desktop(css_text)
    for prop in _LETTERFORM_PROPS:
        side = _resolve(css, _sidebar_wm(theme), prop)
        welc = _resolve(css, _welcome_wm(theme), prop)
        assert side is not None, (
            f'[{theme}] sidebar wordmark resolves no {prop} at all')
        assert side == welc, (
            f'[{theme}] wordmark {prop} DIVERGED: sidebar={side!r} '
            f'welcome={welc!r}. Both surfaces must consume the theme-agnostic '
            f'base-layer source (`.sidebar-header h1 .sidebar-brand, '
            f'.welcome h2.tofu-brand`); a theme may override COLOUR only and a '
            f'surface may override its own font-size only.')


@pytest.mark.parametrize('theme', _THEMES)
def test_wordmark_letter_colour_is_identical_on_both_surfaces(css_text, theme):
    """The letters resolve to the same paint on both surfaces, in every theme.

    This is the assertion that would have caught dark's purple-sidebar /
    gold-welcome split. `background-image` is included because the letters are
    gradient-clipped text — the gradient IS the colour here.
    """
    css = _desktop(css_text)
    for side_cls, welc_cls in _LETTER_PAIRS:
        for prop in ('color', '-webkit-text-fill-color', 'background-image'):
            side = _resolve(css, _sidebar_letter(side_cls, theme), prop)
            welc = _resolve(css, _welcome_letter(welc_cls, theme), prop)
            assert side == welc, (
                f'[{theme}] {prop} diverged for {side_cls} vs {welc_cls}: '
                f'{side!r} vs {welc!r}. One gradient family serves both '
                f'surfaces; a sidebar-only colour override re-opens the drift.')


@pytest.mark.parametrize('theme', _THEMES)
def test_nc_a_sidebar_only_letterspacing_breaks_parity(css_text, theme):
    """NEUTER (in-memory): re-introduce a sidebar-only letter-spacing — the exact
    shape of the drift that shipped — and parity must fail, in every theme.

    Mutates a STRING; the working tree is never written.
    """
    css = _desktop(css_text)
    assert (_resolve(css, _sidebar_wm(theme), 'letter-spacing')
            == _resolve(css, _welcome_wm(theme), 'letter-spacing')), (
        f'[{theme}] baseline is not at parity — nothing to neuter')

    # Appended last AND matching the winning layer's specificity. Under `tofu`
    # the theme block legitimately outranks the base layer, so an unprefixed
    # neuter is silently powerless there — a real trap this suite hit: the
    # neuter must impersonate the layer that actually wins for that theme.
    prefix = f'[data-theme="{theme}"] ' if theme == 'tofu' else ''
    drifted = css + (
        f'\n{prefix}.sidebar-header h1 .sidebar-brand{{letter-spacing:0.04em}}\n')
    side = _resolve(drifted, _sidebar_wm(theme), 'letter-spacing')
    welc = _resolve(drifted, _welcome_wm(theme), 'letter-spacing')
    assert side != welc, (
        f'[{theme}] NC did not bite: a sidebar-only letter-spacing:0.04em still '
        f'resolved equal to the welcome value ({side!r}) — this test is not '
        f'actually reading the sidebar wordmark.')


@pytest.mark.parametrize('theme', _THEMES)
def test_nc_a_sidebar_only_gradient_breaks_colour_parity(css_text, theme):
    """NEUTER (in-memory): restore the purple sidebar gradient that actually
    shipped → letter parity must fail. Proves the colour assertion is
    load-bearing and not just comparing two Nones.

    Under `tofu` the letters are painted flat (the theme blanks the gradient and
    sets a solid --brand-ink), so there the neuter has to attack the property
    that actually carries the colour in THAT theme: the fill.
    """
    css = _desktop(css_text)
    if theme == 'tofu':
        prop = '-webkit-text-fill-color'
        drifted = css + (
            f'\n[data-theme="{theme}"] .sidebar-header h1 .sidebar-brand>span'
            '{-webkit-text-fill-color:#8b6cf6}\n')
        drifted = re.sub(r'\s*>\s*', ' ', drifted)
    else:
        # `light` now carries its own paired darker-amber override, so an
        # unprefixed neuter loses to it and would silently no-op.
        prop = 'background-image'
        prefix = f'[data-theme="{theme}"] ' if theme == 'light' else ''
        drifted = css + (
            f'\n{prefix}.sidebar-brand-t'
            '{background-image:linear-gradient(135deg,#8b6cf6,#a78bfa)}\n')
    side = _resolve(drifted, _sidebar_letter('sidebar-brand-t', theme), prop)
    welc = _resolve(drifted, _welcome_letter('tofu-brand-t', theme), prop)
    assert side != welc, (
        f'[{theme}] NC did not bite: a sidebar-only colour override did not '
        f'break parity (both {side!r}) — the colour assertion is vacuous.')


# ─────────────────────────── 2. the base layer really is the source ──────────

def test_base_layer_source_outranks_the_generic_welcome_h2():
    """The base-layer letterform source MUST outrank `.welcome h2`.

    This is the mechanism, pinned: `.tofu-brand` (0,1,0) lost to `.welcome h2`
    (0,1,1), which silently turned its font-weight/letter-spacing into dead code
    while the sidebar's identical intent applied — that asymmetry IS how the
    drift hid. Both source selectors carry context so they win.
    """
    generic = _specificity('.welcome h2')
    for sel in ('.sidebar-header h1 .sidebar-brand', '.welcome h2.tofu-brand'):
        assert _specificity(sel) > generic, (
            f'{sel} {_specificity(sel)} no longer outranks `.welcome h2` '
            f'{generic}; its declarations would become dead code again.')


def test_letterform_source_is_not_theme_scoped(css_text):
    """At least one rule supplying the letterform must be THEME-AGNOSTIC.

    A `[data-theme=...]`-only source is what shipped the dark/light split, so an
    unprefixed rule carrying font-family for BOTH surfaces has to exist.
    """
    found = False
    for sel, _idx, decls in _iter_rules(_desktop(css_text)):
        if 'data-theme' in sel or 'font-family' not in decls:
            continue
        s = sel.replace(' ', '')
        if '.sidebar-brand' in s or '.tofu-brand' in s:
            found = True
            break
    assert found, (
        'no theme-agnostic rule supplies the wordmark font-family — the source '
        'is theme-scoped again, which is exactly how dark/light drifted.')


def test_font_weight_matches_the_only_shipped_pixelify_face(css_text):
    """Pixelify Sans ships Bold ONLY, so the wordmark weight must be 700 wherever
    that face is used. Asking for 600 makes the browser synthesize, and it does
    so inconsistently across the two font sizes."""
    fonts_css = os.path.join(ROOT, 'static', 'vendor', 'google-fonts-local.css')
    with open(fonts_css, encoding='utf-8') as f:
        faces = set(re.findall(r'PixelifySans-(\w+)\.woff2', f.read()))
    assert faces == {'Bold'}, (
        f'shipped Pixelify faces changed to {faces} — revisit the wordmark '
        f'font-weight (it is pinned to 700 because Bold was the only face).')
    css = _desktop(css_text)
    for theme in ('dark', 'light'):
        for el in (_sidebar_wm(theme), _welcome_wm(theme)):
            fam = (_resolve(css, el, 'font-family') or '').lower()
            weight = _resolve(css, el, 'font-weight')
            if 'pixelify' in fam:
                assert weight == '700', (
                    f'[{theme}] wordmark uses Pixelify at font-weight {weight!r};'
                    f' only Bold (700) is shipped, so anything else is a '
                    f'synthesized face.')


# ─────────────────────────── 3. 方案 A: the `o` is a real letter ─────────────

@pytest.mark.parametrize('theme', _THEMES)
def test_the_o_is_a_visible_letter_on_both_surfaces(css_text, theme):
    """owner 2026-07-28 方案 A: no flat block standing in for the `o`.

    Under tofu the glyph is painted directly, so `color` must not be
    transparent. Under dark/light the letters are gradient-clipped text, where a
    transparent fill is the CORRECT mechanism — so there the requirement is that
    a gradient actually backs the glyph.
    """
    css = _desktop(css_text)
    for name, el in (('sidebar', _sidebar_letter('sidebar-brand-o', theme)),
                     ('welcome', _welcome_letter('tofu-brand-o1', theme))):
        fill = (_resolve(css, el, '-webkit-text-fill-color') or '').strip().lower()
        gradient = (_resolve(css, el, 'background-image') or '').strip().lower()
        painted = fill not in ('transparent', 'rgba(0,0,0,0)', '')
        clipped = 'gradient' in gradient
        assert painted or clipped, (
            f'[{theme}] {name} `o` is neither painted (fill={fill!r}) nor '
            f'gradient-clipped (background-image={gradient!r}) — it is invisible, '
            f'which means a pseudo-element block is standing in for it. '
            f'方案 A requires a real, selectable letter.')


def test_shared_span_rule_outranks_a_bare_letter_class_override():
    """The shared `>span` rule must WIN against a bare per-letter class rule.

    Measured while neuter-testing this suite: an injected
    `[data-theme="tofu"] .sidebar-brand-o{color:transparent}` (0,2,0) did NOT
    turn the `o` transparent, because the context-carrying shared rule outranks
    it. That margin is load-bearing — it is what makes a stray one-letter
    override unable to silently re-open the drift — so it is pinned here rather
    than left as an accident of how the selector happens to be written.
    """
    shared = _specificity(
        '[data-theme="tofu"] .sidebar-header h1 .sidebar-brand>span')
    bare_letter = _specificity('[data-theme="tofu"] .sidebar-brand-o')
    assert shared > bare_letter, (
        f'shared span rule {shared} no longer outranks a bare letter-class rule '
        f'{bare_letter}; a single-letter override could re-introduce the drift.')


def test_no_pseudo_block_paints_the_o_on_either_surface(css_text):
    """The retired gradient-block `o` must not come back: any `::after` on a
    wordmark letter resolves to content:none."""
    offenders = []
    for sel, _idx, decls in _iter_rules(_desktop(css_text)):
        s = sel.replace(' ', '')
        if '::after' not in s:
            continue
        if not any(k in s for k in ('.sidebar-brand-', '.tofu-brand-',
                                    '.sidebar-brand span', '.tofu-brand span')):
            continue
        content = decls.get('content', '').strip().lower()
        if content and content != 'none':
            offenders.append((sel, content))
    assert not offenders, (
        f'a pseudo-element is painting a wordmark letter again: {offenders}. '
        f'方案 A retired the flat block `o`.')


# ─────────────────────────── 4. 落款印 (the clay stamp) ─────────────────────

def test_welcome_stamp_is_a_clay_seal_not_loose_small_text(css_text):
    """`豆腐` is the clay stamp: filled background + a tilt (tofu theme)."""
    css = _desktop(css_text)
    bg = (_resolve(css, _stamp('tofu'), 'background') or '').lower()
    assert 'gradient' in bg, (
        f'welcome 豆腐 background resolved {bg!r} — expected the clay gradient '
        f'seal. It has reverted to loose small text.')
    transform = (_resolve(css, _stamp('tofu'), 'transform') or '').lower()
    assert 'rotate' in transform, (
        f'welcome 豆腐 transform resolved {transform!r} — the seal tilt is gone.')


def test_welcome_stamp_font_size_keeps_a_readable_floor(css_text):
    """The stamp scales with the title (em) but MUST keep a px floor.

    Measured: at the mobile 20px title a pure `.30em` is ~6px and the two Han
    glyphs smudge into one another; a pure px value fails the other way (12.5px
    is nearly as tall as that 20px title). A bare `em` here is a real mobile
    defect, so the floor is load-bearing, not styling taste.
    """
    css = _desktop(css_text)
    size = (_resolve(css, _stamp('tofu'), 'font-size') or '').replace(' ', '').lower()
    assert size.startswith('clamp('), (
        f'welcome 豆腐 font-size resolved {size!r} — expected a clamp() with a '
        f'px floor so it stays readable at the mobile 20px title.')
    floor = re.match(r'clamp\((\d+(?:\.\d+)?)px', size)
    assert floor, f'clamp() floor is not an absolute px value: {size!r}'
    assert float(floor.group(1)) >= 10.0, (
        f'clamp() floor {floor.group(1)}px is below the 10px readability floor '
        f'for two Han glyphs.')


def test_nc_dropping_the_clamp_floor_is_detected(css_text):
    """NEUTER (in-memory): replace the clamp() with the bare `.30em` it protects
    against → the floor assertion's precondition flips."""
    anchor = 'font-size:clamp(10.5px,.30em,13px);'
    assert css_text.count(anchor) == 1, (
        f'NC anchor not unique/found: count={css_text.count(anchor)}')
    neutered = _desktop(css_text.replace(anchor, 'font-size:.30em;', 1))
    size = (_resolve(neutered, _stamp('tofu'), 'font-size') or '').lower()
    assert not size.startswith('clamp('), (
        f'NC did not bite: font-size still resolves {size!r}')


def test_nc_blanking_the_ground_shadow_is_detected(css_text):
    """NEUTER (in-memory): restore the old
    `.welcome-icon::before{content:none}` (which sat LATER in source than the new
    ground shadow and would silently kill it) → the mascot loses its shadow."""
    shadow_sel = '[data-theme="tofu"] .welcome-icon::before'

    def _shadow_content(text: str) -> str:
        best = None
        for sel, _idx, decls in _iter_rules(text):
            if sel.replace(' ', '') != shadow_sel.replace(' ', ''):
                continue
            if 'content' in decls:
                best = decls['content'].strip().lower()
        return best or ''

    assert _shadow_content(_desktop(css_text)) == '""', (
        f'baseline: ground shadow content resolved '
        f'{_shadow_content(_desktop(css_text))!r}, expected \'""\' — the mascot '
        f'has no ground shadow.')
    neutered = _desktop(css_text + '\n' + shadow_sel + '{content:none}\n')
    assert _shadow_content(neutered) == 'none', (
        'NC did not bite: a later content:none did not win, so this test cannot '
        'detect the shadow being blanked.')


# ─────────────────────────── 5. markup contract ─────────────────────────────

def test_both_surfaces_ship_the_four_letter_spans():
    """The shared `>span` rules only reach per-letter spans, so the markup must
    keep them (and the welcome `<small>` that carries the seal)."""
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    for cls in ('sidebar-brand-t', 'sidebar-brand-o', 'sidebar-brand-f',
                'sidebar-brand-u', 'tofu-brand-t', 'tofu-brand-o1',
                'tofu-brand-f', 'tofu-brand-u'):
        assert f'class="{cls}"' in html, f'index.html lost the {cls} span'
    assert '<small>豆腐</small>' in html, 'welcome lost the 豆腐 seal element'


def test_suite_never_writes_the_stylesheet():
    """This suite must never write static/styles.css.

    Writing the live file made a concurrent sibling read it mid-window and the
    suite reported a bogus `''.count` failure; worse, a sibling committing inside
    a restore window would ship NEUTERED CSS. Guarding the guard, since the
    unsafe pattern is easy to reintroduce by copying a sibling suite.
    """
    with open(os.path.abspath(__file__), encoding='utf-8') as f:
        src = f.read()
    body = src.split('def test_suite_never_writes_the_stylesheet', 1)[0]
    assert "open(CSS, 'w'" not in body and 'open(CSS, "w"' not in body, (
        'this suite opens static/styles.css for WRITING — neuters must mutate an '
        'in-memory string instead (shared worktree: a sibling can read a '
        'half-written file, or commit neutered CSS).')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
