#!/usr/bin/env python3
"""Mobile full-screen overlays must NOT size height off bare `100dvh`/`100vh`.

Regression context (2026-07-16): the project-assistant panel
(`.project-modal.pm-workbench`) rendered as a thin BLACK LINE in the Android
WebView. Root cause: the phone block (`@media(max-width:768px)`) forced the
modal full-screen with bare `height:100dvh`, but this WebView measures the
initial-containing-block / `dvh` as 0 (documented at styles.css ~21463 and
main.js `_installViewportHeightGuard`). So the flex column collapsed to ~0px,
leaving only the dark header/border band → a black line. Chrome resolves
`dvh` correctly, so it only reproduced in the app.

The project-wide fix for this WebView-collapse class is to size off the
`--vh100` guard var (published by `_installViewportHeightGuard` as a known-good
pixel height), keeping a plain `100dvh`/`100vh` fallback:
    height: var(--vh100, 100dvh)
The same latent bug was found on `.settings-panel` and the base mobile
`.modal` and fixed in the same pass.

Invariant locked here (env-independent CSS parse): inside the phone block
(`@media(max-width:768px)`), any full-screen overlay rule that pins `height`
(or `max-height`) to a full-viewport `100dvh`/`100vh` MUST route it through
`var(--vh100, …)`. A bare `100dvh`/`100vh` on those props is forbidden — it is
the exact shape that collapses to 0 in the WebView.

NEUTER: rewriting a guarded value back to bare `100dvh` re-trips the assertion.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_CSS_PATH = os.path.join(ROOT, 'static', 'styles.css')

# Full-screen overlay selectors that go edge-to-edge on phones and therefore
# pin height to the full viewport. These are the ones the WebView collapses.
_FULLSCREEN_SELECTORS = ('.modal', '.settings-panel', '.project-modal.pm-workbench')

# Any height / max-height declaration + its value (so we can inspect whether a
# full-viewport value is guarded by --vh100).
_HEIGHT_DECL_RE = re.compile(r'((?:max-)?height):\s*([^;]+)')
# A value that references the full viewport (100dvh / 100vh), guarded or not.
_FULLVIEW_VAL_RE = re.compile(r'100d?vh')


def _phone_block(css: str) -> str:
    """Return the body of the `@media(max-width:768px)` phone block
    (brace-matched, nested rules included)."""
    for m in re.finditer(r'@media[^{]*\{', css):
        opener = m.group(0)
        if 'max-width:768px' not in opener.replace(' ', ''):
            continue
        # Skip compound openers (e.g. coarse/tablet bands) — we want the plain
        # phone block that also carries the fullscreen `.modal` rule.
        i = m.end() - 1
        depth = 0
        for j in range(i, len(css)):
            if css[j] == '{':
                depth += 1
            elif css[j] == '}':
                depth -= 1
                if depth == 0:
                    body = css[i + 1:j]
                    if '.project-modal.pm-workbench' in body:
                        return body
                    break
    raise AssertionError(
        'phone @media(max-width:768px) block containing '
        '.project-modal.pm-workbench not found')


def _rule_bodies(block: str, selector: str):
    """Yield the declaration body of every rule for `selector`, matched at a
    class boundary so `.modal` never matches `.modal-overlay`/`.project-modal`.

    The char BEFORE the selector must not be a `-`/word char/`.` (so `.modal`
    is not seen inside `.project-modal`), and the char AFTER must be `{`, `:`,
    ` `, `,` or `.` (rule open, pseudo, combinator, group, or a further class
    like `.project-modal.pm-workbench`)."""
    esc = re.escape(selector)
    pat = re.compile(r'(?<![\w.\-])' + esc + r'(?=[{:,. ])\s*\{([^{}]*)\}')
    for m in pat.finditer(block):
        yield m.group(1)


def test_fullscreen_overlays_use_vh100_guard_not_bare_dvh():
    css = open(_CSS_PATH, encoding='utf-8').read()
    block = _phone_block(css)
    checked = 0
    for selector in _FULLSCREEN_SELECTORS:
        bodies = list(_rule_bodies(block, selector))
        assert bodies, (
            f'expected a phone-block rule for {selector!r} — the positive test '
            f'is stale (selector renamed or rule moved).')
        for body in bodies:
            for prop, val in _HEIGHT_DECL_RE.findall(body):
                if not _FULLVIEW_VAL_RE.search(val):
                    continue  # not a full-viewport height — irrelevant
                assert '--vh100' in val, (
                    f'{selector!r} sets {prop}:{val.strip()!r} using '
                    f'bare 100dvh/100vh — this collapses to 0 in the Android '
                    f'WebView (initial-containing-block measured as 0). Size it '
                    f'off var(--vh100, 100dvh) like the other full-screen '
                    f'overlays (see styles.css --vh100 guard + '
                    f'main.js _installViewportHeightGuard).')
                checked += 1
    assert checked >= len(_FULLSCREEN_SELECTORS), (
        f'expected to verify a full-viewport height on each of '
        f'{_FULLSCREEN_SELECTORS} but only checked {checked} — the rules may '
        f'have stopped pinning height, making this guard vacuous.')


def test_NC_neuter_bare_dvh_trips_the_guard():
    """NEUTER: rewrite a guarded value back to bare 100dvh → the guard's core
    predicate must flag it."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    block = _phone_block(css)
    bodies = list(_rule_bodies(block, '.project-modal.pm-workbench'))
    assert bodies, 'coarse .project-modal.pm-workbench missing — positive stale'
    body = bodies[0]
    # Emulate the pre-fix state.
    neutered = re.sub(r'height:\s*var\(--vh100,\s*100dvh\)',
                      'height:100dvh', body)
    assert 'height:100dvh' in neutered and 'var(--vh100' not in neutered, (
        'neuter no-op — the current rule is not the expected guarded form')
    # The positive assertion, applied to the neutered body, must now fail.
    prop = re.search(r'((?:max-)?height):\s*([^;]+)', neutered)
    guarded = prop and '--vh100' in prop.group(2)
    assert not guarded, 'the vh-guard invariant must FAIL on bare 100dvh'


if __name__ == '__main__':
    test_fullscreen_overlays_use_vh100_guard_not_bare_dvh()
    test_NC_neuter_bare_dvh_trips_the_guard()
    print('PASS')
