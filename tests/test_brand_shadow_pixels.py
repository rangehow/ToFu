"""Brand-area PIXEL guards — the mascot's ground shadow must actually be PAINTED.

WHY A SEPARATE, BROWSER-BACKED SUITE
------------------------------------
tests/test_brand_wordmark_parity.py resolves the CSS cascade in Python. That is
the right tool for "which rule wins", but it is structurally blind to "did this
reach the screen": a rule can resolve to `content:""` + a gradient and still
paint nothing (clipped by an ancestor, covered by a sibling, zero-sized, or
positioned outside the visible box). The mascot's ground shadow is exactly that
kind of decoration — nothing about it is observable except pixels.

The check is a differential render: screenshot the brand area with the shadow
live, screenshot it again with `.welcome-icon::before{content:none}`, and diff.
Any surviving difference IS the shadow, so the assertion cannot pass vacuously.
Both the pixel count AND the location are asserted — a shadow that renders in
the wrong place (e.g. drifting to a positioned ancestor because `.welcome-icon`
lost `position:relative`) is a defect this must catch, not a pass.

TWO HARNESS TRAPS, both hit while writing this (they are why this file inlines
the stylesheet and settles layout before shooting):

1. STALE / UNPARSED EXTERNAL CSS. Loading `styles.css` via `<link>` from a
   file:// page left `document.styleSheets` unreadable, and probes disagreed
   with what was on screen. Inlining the stylesheet into a `<style>` tag removes
   the loader from the experiment entirely.
2. INJECTING STYLES TOO EARLY. `add_style_tag` immediately after `goto` raced
   the first layout/paint, and the "shadow off" screenshot came back identical
   to "shadow on" — reporting a false ZERO for a shadow that was in fact
   painting ~1300 pixels. Every screenshot here waits for fonts + a settle
   delay, and the neuter is baked into the page's own markup rather than
   injected after load.

Skips (never silently passes) when Chromium is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')
ICON = os.path.join(ROOT, 'static', 'icons', 'tofu-welcome.svg')

# This host's playwright chrome-headless-shell is missing GTK/ATK sonames that
# the conda `tofu` env provides. Mirrors static/icons/_gen/wordmark-preview/shoot.py.
_CONDA_LIB = ('/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/'
              'ruanjunhao04/miniforge3/envs/tofu/lib')

_THEMES = ('tofu', 'dark', 'light')

# The worker runs in a subprocess: LD_LIBRARY_PATH must be set BEFORE the
# process starts, and a crashing browser must not take pytest down with it.
_WORKER = r'''
import json, os, sys
from playwright.sync_api import sync_playwright
from PIL import Image, ImageChops

css_path, icon_path, theme, outdir = sys.argv[1:5]
css = open(css_path, encoding='utf-8').read()

PAGE = """<!doctype html><html lang="zh" data-theme="%s"><head><meta charset="utf-8">
<style>%s</style>
<style>body{margin:0;background:inherit}
 *{animation:none!important;transition:none!important}
 %s</style></head>
<body><div class="welcome"><div class="welcome-icon">
<img src="file://%s" width="64" height="64" alt="Tofu"></div>
<h2 class="tofu-brand"><span class="tofu-brand-t">T</span><span class="tofu-brand-o1">o</span><span
 class="tofu-brand-f">f</span><span class="tofu-brand-u">u</span><small>豆腐</small></h2>
</div></body></html>"""

def shot(kill, tag):
    """Render the brand area. `kill` bakes the neuter into the page itself —
    injecting it after load raced first paint and produced a false zero."""
    extra = '.welcome-icon::before{content:none!important}' if kill else ''
    html = PAGE % (theme, css, extra, icon_path)
    f = os.path.join(outdir, 'p_%s_%s.html' % (theme, tag))
    open(f, 'w', encoding='utf-8').write(html)
    with sync_playwright() as p:
        b = p.chromium.launch(args=['--no-sandbox'])
        pg = b.new_page(viewport={'width': 520, 'height': 400},
                        device_scale_factor=2)
        pg.goto('file://' + f)
        pg.wait_for_load_state('load')
        try:
            pg.evaluate('() => document.fonts.ready')
        except Exception:
            pass
        pg.wait_for_timeout(700)          # settle: layout + paint
        rect = pg.evaluate("""() => {const r =
            document.querySelector('.welcome-icon').getBoundingClientRect();
            return {x:r.x, y:r.y, w:r.width, h:r.height, bottom:r.bottom};}""")
        png = os.path.join(outdir, 'p_%s_%s.png' % (theme, tag))
        pg.screenshot(path=png)
        b.close()
    return png, rect

on, rect = shot(False, 'on')
off, _ = shot(True, 'off')
A = Image.open(on).convert('RGB')
B = Image.open(off).convert('RGB')
d = ImageChops.difference(A, B)
bbox = d.getbbox()
px = sum(1 for q in d.getdata() if sum(q) > 3)
print('RESULT' + json.dumps({'px': px, 'bbox': bbox, 'rect': rect, 'dpr': 2}))
'''


def _chromium_available() -> bool:
    try:
        import playwright  # noqa: F401
    except Exception:
        return False
    return os.path.isdir(_CONDA_LIB) or shutil.which('google-chrome') is not None


@pytest.fixture(scope='module')
def _worker_path():
    fd, path = tempfile.mkstemp(suffix='_shadow_worker.py')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(_WORKER)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _render_diff(theme: str, worker: str) -> dict:
    """Return {'px', 'bbox', 'rect', 'dpr'} for shadow-on vs shadow-off."""
    env = dict(os.environ)
    if _CONDA_LIB not in env.get('LD_LIBRARY_PATH', ''):
        env['LD_LIBRARY_PATH'] = _CONDA_LIB + ':' + env.get('LD_LIBRARY_PATH', '')
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run([sys.executable, worker, CSS, ICON, theme, td],
                           capture_output=True, text=True, env=env, timeout=180)
    for line in (r.stdout or '').splitlines():
        if line.startswith('RESULT'):
            import json
            return json.loads(line[len('RESULT'):])
    pytest.skip(f'headless Chromium unavailable / worker failed: '
                f'{(r.stderr or r.stdout or "")[-400:]}')


@pytest.mark.parametrize('theme', _THEMES)
def test_mascot_ground_shadow_is_actually_painted(theme, _worker_path):
    """The ground shadow must produce VISIBLE pixels, in every theme.

    Declaration-level guards cannot see this: the CSS resolved correctly the
    whole time the shadow was being investigated, so only a render proves it.
    """
    if not _chromium_available():
        pytest.skip('playwright/chromium not available on this host')
    res = _render_diff(theme, _worker_path)
    assert res['px'] > 0, (
        f'[{theme}] the mascot ground shadow painted ZERO pixels — it is dead '
        f'decoration. The CSS may resolve fine while the ellipse is clipped, '
        f'covered by the mascot image, or positioned outside the visible box.')


@pytest.mark.parametrize('theme', _THEMES)
def test_ground_shadow_sits_under_the_mascot(theme, _worker_path):
    """The shadow must render UNDER the mascot: horizontally within the icon
    box, and vertically at its bottom edge.

    Pixel-count alone would accept a shadow that escaped to a positioned
    ancestor — precisely the failure mode `.welcome-icon{position:relative}`
    exists to prevent, so the location is asserted too.
    """
    if not _chromium_available():
        pytest.skip('playwright/chromium not available on this host')
    res = _render_diff(theme, _worker_path)
    assert res['px'] > 0, f'[{theme}] no shadow pixels at all'
    dpr = res['dpr']
    x0, y0, x1, y1 = (v / dpr for v in res['bbox'])
    r = res['rect']
    icon_left, icon_right = r['x'], r['x'] + r['w']
    # Horizontal: inside the icon box (small tolerance for the blur falloff).
    assert x0 >= icon_left - 12 and x1 <= icon_right + 12, (
        f'[{theme}] shadow spans x[{x0:.0f}..{x1:.0f}] but the icon is '
        f'x[{icon_left:.0f}..{icon_right:.0f}] — it is not under the mascot.')
    # Vertical: at the mascot's feet, not up around its head.
    assert y1 >= r['y'] + r['h'] * 0.5, (
        f'[{theme}] shadow bottom y={y1:.0f} is above the icon mid-line '
        f'(icon y={r["y"]:.0f} h={r["h"]:.0f}) — a ground shadow belongs at the '
        f'feet.')
    assert abs(y1 - r['bottom']) < 24, (
        f'[{theme}] shadow bottom y={y1:.0f} is {abs(y1 - r["bottom"]):.0f}px '
        f'from the icon bottom ({r["bottom"]:.0f}) — it has drifted away from '
        f'the mascot, most likely anchoring to a different positioned ancestor.')


def test_the_pixel_probe_can_actually_fail(_worker_path):
    """NEUTER for the probe itself: with the shadow hidden on BOTH renders the
    diff must be empty, proving a non-zero count means something.

    Without this, `px > 0` could be satisfied by any incidental difference
    between the two screenshots (font jitter, animation phase) and the suite
    would pass even if the shadow never painted.
    """
    if not _chromium_available():
        pytest.skip('playwright/chromium not available on this host')
    env = dict(os.environ)
    if _CONDA_LIB not in env.get('LD_LIBRARY_PATH', ''):
        env['LD_LIBRARY_PATH'] = _CONDA_LIB + ':' + env.get('LD_LIBRARY_PATH', '')
    # Feed the worker a stylesheet whose shadow is disabled: on/off are then the
    # same page, so an honest probe reports exactly zero.
    css = open(CSS, encoding='utf-8').read()
    css += '\n.welcome-icon::before{content:none!important}\n'
    with tempfile.TemporaryDirectory() as td:
        neutered = os.path.join(td, 'styles_neutered.css')
        with open(neutered, 'w', encoding='utf-8') as f:
            f.write(css)
        r = subprocess.run([sys.executable, _worker_path, neutered, ICON,
                            'tofu', td],
                           capture_output=True, text=True, env=env, timeout=180)
        payload = None
        for line in (r.stdout or '').splitlines():
            if line.startswith('RESULT'):
                import json
                payload = json.loads(line[len('RESULT'):])
        if payload is None:
            pytest.skip('headless Chromium unavailable for the probe neuter')
    assert payload['px'] == 0, (
        f'probe is not trustworthy: with the shadow disabled in BOTH renders it '
        f'still reported {payload["px"]} differing pixels — the non-zero counts '
        f'elsewhere in this suite could be render noise rather than the shadow.')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
