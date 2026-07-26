#!/usr/bin/env python3
"""Why i18n step ③ cannot ship without ONE new server-visible input.

STATUS: this is the blocking analysis for Epic-E sub-part 1 step ③. Steps ①
and ② landed (4fbab4fa observability; the boot-sync shape guard). Step ③ — the
actual split — is blocked on a contradiction between two facts that are BOTH
currently true, and this file pins both so the contradiction cannot be
forgotten or hand-waved past.

THE CONTRADICTION
-----------------
(A) The boot language MUST ship eagerly, inside the core bundle, present
    before ``_applyI18n()`` runs. Established by
    tests/test_i18n_pack_boot_sync.py: index.html has 309 data-i18n bindings
    whose static fallback is not uniformly one language (~69% / ~88% CJK
    across the two channels), so a late dictionary flashes the wrong language
    at the DEFAULT user on every cold load.

(B) The server cannot know which language to ship. The language lives in
    ``localStorage['tofu_ui_lang']`` (i18n.js), which is read ZERO times
    anywhere under routes/ or lib/. ``index_page()`` caches the rendered HTML
    keyed by (bundle_tag, styles_tag, feature_tag, mtime, panels) — no
    language dimension — and ``build_bundle()`` produces exactly ONE
    content-addressed ``bundle-<hash>.js`` for all clients.

A shipped-eagerly pack must be chosen at bundle/serve time (A), by a server
that has no way to choose (B). Every resolution requires giving the server a
language signal it does not have today:

  * a ``lang`` cookie the server reads     -> new server-visible input
  * Accept-Language negotiation            -> new server-visible input
    (and wrong: it is the BROWSER's locale, not this app's setting)
  * two bundles + client picks at boot     -> the pick is a second network
    round trip BEFORE _applyI18n(), i.e. exactly the async boot (A) forbids
  * inline the boot pack into the HTML     -> the HTML is what must vary by
    language, so this is the cookie option wearing a hat

The deferred-bundle mechanism (feature-<hash>.js, fetched at runtime by
feature-loader.js) is NOT a counter-example: it is explicitly for modules that
are *not* needed at first paint. The boot dictionary is the opposite case.

WHY THIS IS A REAL GATE, NOT A DESIGN PREFERENCE
------------------------------------------------
The dispatching ticket said the split needs "no contract change, the language
can stay in localStorage". That is true for the OTHER language (fetched inside
setLanguage(), a user-initiated action). It is NOT true for the boot language.
Shipping step ③ as specified would mean either an async boot pack — a
first-paint correctness regression for the default user — or silently adding
the very server-visible language input the ticket says is unnecessary.

So the honest move is to surface the choice rather than smuggle it in. These
tests fail the moment either half of the contradiction stops holding, which is
exactly when step ③ becomes implementable.

Run: python3 tests/test_i18n_split_blocked_on_lang_signal.py
"""

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N = os.path.join(REPO, 'static', 'js', 'i18n.js')
COMMON = os.path.join(REPO, 'routes', 'common.py')
BUNDLER = os.path.join(REPO, 'lib', 'js_bundler.py')

try:
    import pytest
except ImportError:
    pytest = None


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


def _read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


@_unit
def test_language_lives_only_in_localStorage():
    """Half (B), part 1: the language's only home is the browser."""
    src = _read(I18N)
    assert "localStorage.getItem('tofu_ui_lang')" in src, (
        'the language source moved; if it is now server-visible, step ③ is '
        'unblocked and this whole file should be deleted')


@_unit
def test_server_never_reads_the_language_key():
    """Half (B), part 2: no route or lib consults it.

    Uses git grep (fast, respects the index) over the Python surface only —
    a hit under static/js is the CLIENT reading its own key, which is fine.
    """
    try:
        r = subprocess.run(
            ['git', 'grep', '-l', 'tofu_ui_lang', '--', 'routes/', 'lib/', 'server.py'],
            cwd=REPO, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        print(f'SKIP (git grep unavailable: {e})')
        return
    hits = [l for l in r.stdout.splitlines() if l.strip()]
    assert not hits, (
        f'server-side code now reads tofu_ui_lang ({hits}) — the language IS '
        f'server-visible, so the eager per-language bundle became possible and '
        f'step ③ is unblocked')


@_unit
def test_served_html_is_not_cached_per_language():
    """Half (B), part 3: one rendered HTML for every client."""
    src = _read(COMMON)
    m = re.search(r'_bundled_index_cache = \{([^}]*)\}', src)
    assert m, 'could not locate the index HTML cache'
    key = m.group(1)
    assert 'lang' not in key.lower(), (
        'the served-HTML cache gained a language dimension — that is the '
        'server-visible signal step ③ needs; re-open the design')


@_unit
def test_core_bundle_is_one_artifact_for_all_clients():
    """Half (B), part 4: build_bundle() emits a single core bundle."""
    src = _read(BUNDLER)
    m = re.search(r"_assemble_bundle\(_BUNDLE_FILES,\s*'bundle-'", src)
    assert m, (
        'the core bundle assembly changed shape — if it now emits a variant '
        'per language, step ③ is unblocked')


@_unit
def test_boot_sync_requirement_is_still_pinned_elsewhere():
    """Half (A): the companion guard must still exist and still assert it.

    If someone deletes that file, this analysis silently loses half its
    premise — so depend on it explicitly rather than restating its evidence.
    """
    companion = os.path.join(REPO, 'tests', 'test_i18n_pack_boot_sync.py')
    assert os.path.exists(companion), (
        'test_i18n_pack_boot_sync.py is gone — the boot-must-be-synchronous '
        'half of this contradiction is no longer pinned anywhere')
    body = _read(companion)
    assert 'i18n.js' in body and '_BUNDLE_FILES' in body, (
        'the companion guard no longer asserts eager-load ordering')


@_unit
def test_the_other_language_seam_remains_viable():
    """The half that IS unblocked, so a future implementer starts there.

    setLanguage() is user-initiated and repaints in place, so fetching the
    non-boot pack there costs nothing perceptible. That part of step ③ needs
    no new server input at all.
    """
    src = _read(I18N)
    m = re.search(r'function setLanguage\(lang\)\s*\{(.*?)\n\}', src, re.S)
    assert m, 'setLanguage not found'
    body = m.group(1)
    assert '_applyI18n()' in body and 'location.reload' not in body, (
        'setLanguage is no longer an in-place repaint — the on-demand pack '
        'fetch for the NON-boot language would need rework')
    assert re.search(r'^var _i18n = \{', src, re.M), (
        'the dictionary is no longer a mutable `var` — a fetched pack could '
        'not be merged in')


if __name__ == '__main__':
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('ok  ', name)
            except AssertionError as e:
                failures += 1
                print('FAIL', name)
                print('     ', e)
    print('ALL PASSED' if not failures else f'{failures} FAILED')
    sys.exit(1 if failures else 0)
