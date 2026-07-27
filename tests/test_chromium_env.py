#!/usr/bin/env python3
"""Guard tests: headless Chromium must be launchable from ANY entry point.

WHAT BROKE (measured 2026-07-28, not inferred)
----------------------------------------------
A plain ``python3`` in this repo could not screenshot at all::

    chrome-headless-shell: error while loading shared libraries:
    libatk-1.0.so.0: cannot open shared object file

...while all 12 GUI libs were present in the env prefix the whole time. The
libs were never missing; ``LD_LIBRARY_PATH`` simply was not exported, because
each of the four hand-copied exporters keyed off a signal weaker than
``sys.prefix``:

  * ``server.py`` / ``bootstrap.py`` → ``.tofu_env.json``, a gitignored per-host
    marker that export.py:348 deliberately STRIPS from every bundle. So the
    exported product — what other users install — had no working browser.
  * ``tests/conftest.py`` and ``tofu_search``'s pool → ``$CONDA_PREFIX``, unset
    in any non-activated shell and never set on the uv/venv path. Measured:
    with it unset, the self-heal is a no-op and launch still dies.

WHY THESE ASSERT RESULTS, NOT IMPLEMENTATION
--------------------------------------------
Charter: "行为守卫 MUST 断言结果". The pre-existing guards in
``test_install_uv_fastpath.py`` assert that server.py CONTAINS certain source
text. Those are legitimate ratchets, but they cannot notice that the exported
product has no marker to read — they were all GREEN while screenshots were
100% dead from a bare shell. So the tests here launch a REAL browser in a
SCRUBBED environment and assert bytes come back.

Judged by: "if the production logic were deleted today, would this go red?"
Yes — remove the export and the scrubbed-env launch fails.
"""

import os
import shutil
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chromium_env  # noqa: E402


# ── The env a fresh user actually has ────────────────────────────────
# Scrubbing these reproduces the three real-world shapes in ONE fixture:
# a fresh clone / exported bundle (no marker), a non-activated shell (no
# CONDA_PREFIX), and a Docker/pip layout (neither).
_SCRUBBED = ('LD_LIBRARY_PATH', 'CONDA_PREFIX', 'CONDA_DEFAULT_ENV',
             'FONTCONFIG_PATH', 'FONTCONFIG_FILE', 'CHROMIUM_EXTRA_LIB_DIRS')


def _scrubbed_env():
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    env['PYTHONPATH'] = ROOT
    return env


def _run_probe(body, env=None):
    """Run ``body`` in a subprocess with a scrubbed env; return (rc, out)."""
    proc = subprocess.run(
        [sys.executable, '-c', textwrap.dedent(body)],
        cwd=ROOT, env=env if env is not None else _scrubbed_env(),
        capture_output=True, text=True, timeout=300)
    return proc.returncode, (proc.stdout + proc.stderr)


def _have_playwright():
    try:
        import playwright  # noqa: F401
    except Exception:
        return False
    return True


def _chromium_present():
    """True when a Playwright browser build is actually on disk.

    Distinguishes "not installed" (legitimate skip) from "installed but cannot
    launch" (the defect this file guards). Without this split a broken env
    would silently skip instead of failing.
    """
    base = os.path.expanduser('~/.cache/ms-playwright')
    if not os.path.isdir(base):
        return False
    for entry in os.listdir(base):
        if not entry.startswith(('chromium-', 'chromium_headless_shell-')):
            continue
        for rel in ('chrome-linux64/chrome', 'chrome-linux/chrome',
                    'chrome-headless-shell-linux64/chrome-headless-shell'):
            if os.path.isfile(os.path.join(base, entry, rel)):
                return True
    return False


requires_browser = pytest.mark.skipif(
    not _have_playwright() or not _chromium_present(),
    reason='playwright/chromium not installed (run: python -m playwright '
           'install chromium) — nothing to guard on this host')


# ── Scan-surface report (charter: print what you actually cover) ─────

def test_scan_surface_report():
    """Print the resolved env BEFORE any assertion.

    Charter ("扫描类守卫必须先验证扫描面"): a guard whose inputs are silently
    empty passes while covering nothing. This makes the coverage auditable.
    """
    desc = chromium_env.describe_chromium_env()
    print('\n  sys.prefix          :', sys.prefix)
    print('  GUI lib dirs found  :', desc['lib_dirs'] or '<none>')
    print('  system /etc/fonts   :', desc['system_fontconfig'])
    print('  fontconfig fallback :', desc['fontconfig'] or '<none>')
    print('  issues              :', desc['issues'] or '<none>')
    print('  playwright installed:', _have_playwright())
    print('  chromium on disk    :', _chromium_present())
    assert isinstance(desc['lib_dirs'], list)


# ── The behavioural core ─────────────────────────────────────────────

@requires_browser
def test_screenshot_works_from_a_scrubbed_environment():
    """THE regression: a real screenshot with no marker and no CONDA_PREFIX.

    This is the exact shape a user of the EXPORTED bundle has. It fails on the
    pre-fix tree with "libatk-1.0.so.0: cannot open shared object file".
    """
    rc, out = _run_probe('''
        import os, sys
        assert not os.environ.get('LD_LIBRARY_PATH'), 'fixture leaked LD_LIBRARY_PATH'
        assert not os.environ.get('CONDA_PREFIX'), 'fixture leaked CONDA_PREFIX'
        from chromium_env import ensure_chromium_env
        ensure_chromium_env()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=['--no-sandbox'])
            pg = b.new_page(viewport={'width': 400, 'height': 200})
            pg.set_content("<b style='font:28px sans-serif'>tofu</b>")
            png = pg.screenshot()
            b.close()
        assert len(png) > 500, 'screenshot suspiciously small: %d' % len(png)
        print('SCREENSHOT_BYTES=%d' % len(png))
    ''')
    assert rc == 0, f'screenshot failed in a scrubbed env:\n{out[-2500:]}'
    assert 'SCREENSHOT_BYTES=' in out, out[-2000:]


@requires_browser
def test_text_actually_renders_not_blank_but_styled():
    """Fonts must resolve — the silent half of the failure.

    With no ``/etc/fonts`` and no fontconfig fallback, Chromium launches and
    paints CSS but draws every glyph as nothing: screenshots come back
    blank-but-styled, which reads as "the page didn't load" rather than as an
    error. A launch-only assertion cannot see this, so measure a glyph.
    """
    rc, out = _run_probe('''
        from chromium_env import ensure_chromium_env
        ensure_chromium_env()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=['--no-sandbox'])
            pg = b.new_page()
            pg.set_content('<h1>tofu</h1>')
            w = pg.evaluate("(()=>{const c=document.createElement('canvas')"
                            ".getContext('2d');c.font='60px sans-serif';"
                            "return c.measureText('tofu').width;})()")
            b.close()
        assert w and w > 0, 'zero-width glyphs: Chromium found NO fonts'
        print('GLYPH_WIDTH=%.1f' % w)
    ''')
    assert rc == 0, f'font probe failed:\n{out[-2500:]}'
    assert 'GLYPH_WIDTH=' in out, out[-2000:]


def test_resolution_does_not_depend_on_the_env_marker():
    """``.tofu_env.json`` is stripped from every export (export.py:348).

    So resolution must work from ``sys.prefix`` alone. Asserted as a RESULT:
    with the marker made unreadable, the same dirs still resolve.
    """
    from_prefix = chromium_env.chromium_lib_dirs()
    with_bogus = chromium_env.chromium_lib_dirs(env_prefix='/nonexistent/prefix')
    assert from_prefix == with_bogus, (
        'a bogus marker prefix changed resolution — the marker is not '
        f'supposed to be load-bearing: {from_prefix} != {with_bogus}')
    if chromium_env._dir_carries_gui_libs(os.path.join(sys.prefix, 'lib')):
        assert os.path.join(sys.prefix, 'lib') in from_prefix, \
            'sys.prefix/lib carries the GUI libs but was not resolved'


def test_only_directories_with_real_libs_are_added(tmp_path):
    """Evidence, not inference: a lib-less prefix must NOT be added.

    Prevents the pattern that let the four copies drift — assuming a
    conda-shaped prefix must have usable libs.
    """
    empty = tmp_path / 'empty-prefix'
    (empty / 'lib').mkdir(parents=True)
    env = {}
    chromium_env.ensure_chromium_env(env=env, env_prefix=str(empty))
    assert str(empty / 'lib') not in env.get('LD_LIBRARY_PATH', ''), \
        'a directory with no GUI libs was added to LD_LIBRARY_PATH'

    fake = tmp_path / 'real-prefix'
    (fake / 'lib').mkdir(parents=True)
    (fake / 'lib' / 'libatk-1.0.so.0').write_bytes(b'\x7fELF')
    env2 = {}
    chromium_env.ensure_chromium_env(env=env2, env_prefix=str(fake))
    if sys.platform.startswith('linux'):
        assert str(fake / 'lib') in env2.get('LD_LIBRARY_PATH', ''), \
            'a directory that DOES carry libatk was not added'


def test_existing_paths_are_preserved_and_idempotent(tmp_path):
    """Never clobber an operator's own LD_LIBRARY_PATH; never grow unbounded."""
    fake = tmp_path / 'p'
    (fake / 'lib').mkdir(parents=True)
    (fake / 'lib' / 'libnss3.so').write_bytes(b'\x7fELF')
    env = {'LD_LIBRARY_PATH': '/opt/mine/lib'}
    chromium_env.ensure_chromium_env(env=env, env_prefix=str(fake))
    first = env['LD_LIBRARY_PATH']
    assert '/opt/mine/lib' in first, 'operator LD_LIBRARY_PATH was dropped'
    chromium_env.ensure_chromium_env(env=env, env_prefix=str(fake))
    assert env['LD_LIBRARY_PATH'] == first, 'not idempotent — path grew'


def test_system_fontconfig_is_never_overridden(tmp_path, monkeypatch):
    """A host WITH /etc/fonts must keep using it (regression direction #2).

    The complement of the fallback test: without this, "always export our own
    config" would also pass, and we would break every normal Linux desktop.
    """
    monkeypatch.setattr(os.path, 'isdir',
                        lambda p: True if p == '/etc/fonts' else os.path.lexists(p))
    conf_dir, conf_file = chromium_env.fontconfig_paths()
    assert conf_dir is None and conf_file is None, \
        'fontconfig fallback fired even though /etc/fonts exists'


def test_operator_fontconfig_file_wins(tmp_path):
    """An explicitly-set FONTCONFIG_FILE must not be replaced."""
    env = {'FONTCONFIG_FILE': '/my/own/fonts.conf'}
    chromium_env.ensure_chromium_env(env=env)
    assert env['FONTCONFIG_FILE'] == '/my/own/fonts.conf'


def test_module_imports_without_dragging_third_party():
    """``server.py`` calls this BEFORE its third-party imports.

    ``import lib.log`` transitively pulls ``lib/__init__`` (measured 0.44 s,
    405 modules, incl. requests), so this module must stay stdlib-only at
    import time. Asserted as a RESULT: import it in a virgin interpreter and
    check what landed in sys.modules.
    """
    rc, out = _run_probe('''
        import sys
        import chromium_env  # noqa: F401
        heavy = [m for m in ('requests', 'quart', 'httpx', 'sqlalchemy',
                             'playwright', 'fitz', 'PIL', 'lib', 'lib.log')
                 if m in sys.modules]
        assert not heavy, 'chromium_env dragged in %s at import time' % heavy
        print('CLEAN_IMPORT')
    ''')
    assert rc == 0, out[-2000:]
    assert 'CLEAN_IMPORT' in out, out[-1500:]


def test_conftest_browser_fixture_helper_actually_runs():
    """The visual-E2E ``browser`` fixture's helper must RUN, not just parse.

    Found the hard way: my first rewrite of ``_ensure_chromium_library_path``
    used ``sys`` without importing it in conftest.py, so every
    ``@pytest.mark.visual`` test ERRORed with ``NameError: name 'sys' is not
    defined``. None of the other tests here caught it, because they exercise
    the shared module directly and never go through conftest's wrapper.

    So call the wrapper for real. A NameError / ImportError inside it fails
    here instead of taking out the whole visual suite.

    Loaded by PATH rather than by module name: ``conftest`` is not importable
    as a top-level module under this rootdir, and a guard that dies on
    ImportError is red in every state, which is just as useless as one that is
    green in every state.
    """
    import importlib.util
    path = os.path.join(ROOT, 'tests', 'conftest.py')
    spec = importlib.util.spec_from_file_location('_cf_probe', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    added = mod._ensure_chromium_library_path()
    assert isinstance(added, list), \
        f'_ensure_chromium_library_path returned {type(added).__name__}, not a list'


def test_all_entry_points_use_the_shared_module():
    """Ratchet: no entry point may re-grow its own copy.

    Anchored on the AST \u2014 a real ``import chromium_env`` / ``from chromium_env
    import \u2026`` node \u2014 NOT on the substring. NEUTER-4 caught the substring
    version passing after the CALL was deleted, because four mentions survived
    in comments and docstrings; a comment must never be able to satisfy a
    guard. (Charter: "\u6d4b\u4e86 helper \u4e0d\u7b49\u4e8e\u6d4b\u4e86\u8c03\u7528\u70b9" \u2014 judged by "delete this call:
    does the guard go red?")

    This is the one implementation-facing assertion in this file and it is a
    ratchet by design: four independent drifting copies is the exact failure
    being closed here.
    """
    import ast

    consumers = {
        'server.py': 'boots the app',
        'bootstrap.py': 'smart launcher',
        'tests/conftest.py': 'visual E2E browser fixture',
        'lib/motion_video/_env.py': 'video render chain',
    }
    missing = []
    for rel, why in consumers.items():
        with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=rel)
        imported = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == 'chromium_env':
                imported = True
            elif isinstance(node, ast.Import):
                if any(a.name == 'chromium_env' for a in node.names):
                    imported = True
        if not imported:
            missing.append(f'{rel} ({why})')
    assert not missing, (
        'these entry points contain no real import of chromium_env.py, so they '
        f'have their own drifting copy: {missing}')
