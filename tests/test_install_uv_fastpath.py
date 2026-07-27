#!/usr/bin/env python3
"""Guard tests: install.sh's uv fast path + clean conda fallback.

Background — the "install is too slow" optimization (2026-07):
  The historical bottleneck was the conda-forge solve (build a full env +
  resolve 40+ packages, minutes). Measured data showed `uv venv` + `uv pip
  install -r requirements.txt` installs the same stack from prebuilt
  manylinux wheels in ~1-2 min with ZERO from-source builds. So install.sh
  now defaults to a uv fast path and falls back to the (unchanged) conda
  path when it can't or shouldn't use uv.

  The compatibility FLOOR is that the conda fallback must always remain
  reachable — CentOS7 / old-glibc hosts (where PyMuPDF/Pillow ship no
  manylinux2014 wheel) must land on conda cleanly. These tests pin the
  branch structure by static analysis (no network, no uv, no conda, no
  server) so the fast path can never silently swallow the fallback.
"""

import os
import re
import sys

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _install_sh() -> str:
    with open(os.path.join(ROOT, 'install.sh'), 'r', encoding='utf-8') as f:
        return f.read()


def _server_py() -> str:
    with open(os.path.join(ROOT, 'server.py'), 'r', encoding='utf-8') as f:
        return f.read()


def _bootstrap_py() -> str:
    with open(os.path.join(ROOT, 'bootstrap.py'), 'r', encoding='utf-8') as f:
        return f.read()


def test_use_conda_flag_parses_and_defaults_off():
    """--use-conda must parse to USE_CONDA=1 and default to 0."""
    text = _install_sh()
    assert 'USE_CONDA=0' in text, 'USE_CONDA has no default-off init'
    assert '--use-conda)' in text, 'no --use-conda argument parser'
    assert re.search(r'--use-conda\)\s+USE_CONDA=1', text), \
        '--use-conda does not set USE_CONDA=1'
    _ok('--use-conda parses to USE_CONDA=1 and defaults off')


def test_uv_path_gated_on_glibc_and_flags():
    """The uv attempt must be gated: --use-conda / --with-postgres / glibc<2.28
    all force the conda path; only otherwise is _try_uv_install called."""
    text = _install_sh()
    # A glibc>=2.28 probe exists.
    assert '_glibc_ge_228' in text, 'no _glibc_ge_228 probe defined'
    assert re.search(r'a\[1\]>2\|\|\(a\[1\]==2&&a\[2\]>=28\)', text), \
        '_glibc_ge_228 does not compare against 2.28'
    # The decision chain: USE_CONDA short-circuits; --with-postgres → conda;
    # glibc<2.28 → conda; else run _try_uv_install.
    chain = text[text.index('if [[ "$USE_CONDA" -eq 1 ]]; then'):]
    assert re.search(r'if \[\[ "\$USE_CONDA" -eq 1 \]\]; then', chain), \
        'decision chain does not start with the USE_CONDA short-circuit'
    assert re.search(r'elif \[\[ "\$WITH_POSTGRES" -eq 1 \]\]; then.*?USE_CONDA=1',
                     chain, re.S), '--with-postgres does not auto-switch to conda'
    assert re.search(r'elif ! _glibc_ge_228; then.*?USE_CONDA=1', chain, re.S), \
        'glibc<2.28 does not force the conda path'
    assert re.search(r'else\s+.*?_try_uv_install', chain, re.S), \
        '_try_uv_install is not the else (default) branch'
    _ok('uv attempt is gated on --use-conda / --with-postgres / glibc>=2.28')


def test_uv_fallback_is_clean_and_smoke_tests_glibc_canaries():
    """_try_uv_install must return non-zero on failure (→ conda) and its import
    smoke-test must include the glibc-floor canaries fitz (PyMuPDF) + PIL."""
    text = _install_sh()
    fn = text[text.index('_try_uv_install() {'):text.index('\nif [[ "$USE_CONDA" -eq 1 ]]; then')]
    # Every failure path returns 1 (never fail()/exit).
    assert fn.count('return 1') >= 4, \
        '_try_uv_install has too few `return 1` fallbacks (failures must fall to conda)'
    assert 'fail ' not in fn and 'exit 1' not in fn, \
        '_try_uv_install must never fail()/exit — a uv failure is recoverable'
    # The smoke-test line imports the glibc-floor canaries.
    assert re.search(r"-c 'import [^']*\bfitz\b[^']*\bPIL\b", fn) or \
           re.search(r"-c 'import [^']*\bPIL\b[^']*\bfitz\b", fn), \
        'import smoke-test does not exercise BOTH fitz (PyMuPDF) and PIL (Pillow)'
    # A failed smoke-test triggers the fallback.
    assert re.search(r'import smoke-test.*?return 1', fn, re.S) or \
           re.search(r'falling back to conda"\s*\n\s*return 1', fn), \
        'a failed import smoke-test does not return 1 (fall back to conda)'
    # The caller treats a non-zero return as "continue with conda".
    assert re.search(r'if _try_uv_install; then\s*\n\s*_FAST_PATH_DONE=1',
                     text), 'caller does not set _FAST_PATH_DONE only on uv success'
    assert re.search(r'else\s*\n\s*warn "uv fast path did not complete', text), \
        'caller does not warn+continue to conda when uv fails'
    _ok('uv fallback is clean: return 1 on any failure incl. fitz/PIL smoke-test')


def test_conda_pipeline_guarded_by_fast_path_flag():
    """Steps 1–8 (the conda pipeline) must sit under the _FAST_PATH_DONE guard
    and the guard must close before Step 8.5, so uv skips conda wholesale."""
    text = _install_sh()
    open_idx = text.index('if [[ "$_FAST_PATH_DONE" -ne 1 ]]; then')
    close_idx = text.index('fi  # ── end legacy conda path')
    step1 = text.index('#  Step 1: Locate, version-check, or install conda')
    step85 = text.index('#  Step 8.5: Validate data/pgdata')
    # Ordering: guard-open < Step1 < guard-close < Step 8.5.
    assert open_idx < step1 < close_idx < step85, \
        'conda pipeline is not fully wrapped by the _FAST_PATH_DONE guard'
    # The heavy conda solves live INSIDE the guarded region.
    region = text[open_idx:close_idx]
    assert 'conda create -n "$ENV_NAME"' in region, \
        'conda env creation escaped the guard'
    assert 'Installing Python dependencies from conda-forge' in region, \
        'the conda-forge dep install escaped the guard'
    _ok('the conda pipeline (Steps 1–8) is guarded by _FAST_PATH_DONE')


def test_conda_only_globals_preseeded_for_set_u():
    """CONDA_BASE / CONDA_OWNED_BY_US are referenced by the shared launch tail;
    they must be pre-seeded before the guard so `set -u` never trips on the uv
    path (where the conda block that sets them is skipped)."""
    text = _install_sh()
    guard = text.index('if [[ "$_FAST_PATH_DONE" -ne 1 ]]; then')
    pre = text[:guard]
    assert 'CONDA_BASE="${CONDA_BASE:-}"' in pre, \
        'CONDA_BASE not pre-seeded before the conda guard (set -u hazard on uv path)'
    assert 'CONDA_OWNED_BY_US="${CONDA_OWNED_BY_US:-0}"' in pre, \
        'CONDA_OWNED_BY_US not pre-seeded before the conda guard'
    _ok('conda-only globals are pre-seeded so the uv path is set -u-safe')


def test_uv_venv_uses_managed_python():
    """uv venv must be seeded from uv's OWN managed CPython so .venv/bin/python
    resolves to a distinct base binary (avoids the re-exec symlink-collision)."""
    text = _install_sh()
    assert '--python-preference only-managed' in text, \
        'uv venv does not force --python-preference only-managed (symlink-collision hazard)'
    _ok('uv venv is seeded from a managed standalone CPython')


def test_reexec_uses_prefix_not_just_executable():
    """server.py + bootstrap.py must decide 'already in env' by comparing
    sys.prefix to env_prefix (a venv's bin/python is a symlink to a base
    interpreter, so a bare executable compare can false-positive)."""
    for src, name in ((_server_py(), 'server.py'), (_bootstrap_py(), 'bootstrap.py')):
        assert re.search(r'os\.path\.realpath\(sys\.prefix\) == os\.path\.realpath\(env_prefix\)',
                         src), f'{name} does not use a sys.prefix vs env_prefix re-exec check'
    _ok('re-exec guard compares sys.prefix to env_prefix (symlink-safe) in both consumers')


def test_uv_marker_has_backend_field():
    """The uv path must write .tofu_env.json with backend='uv' + venv prefix."""
    text = _install_sh()
    fn = text[text.index('_try_uv_install() {'):text.index('\nif [[ "$USE_CONDA" -eq 1 ]]; then')]
    assert "'backend': 'uv'" in fn, "uv marker does not record backend='uv'"
    assert "'python': env_python" in fn, 'uv marker does not record the venv python'
    assert "'env_prefix': env_prefix" in fn, 'uv marker does not record env_prefix'
    _ok("uv path writes .tofu_env.json with backend='uv'")


def test_uv_ripgrep_no_source_build():
    """The uv path must NOT pip-install ripgrep (needs cargo); it detects the
    system binary and degrades to the Python fallback otherwise."""
    text = _install_sh()
    fn = text[text.index('_try_uv_install() {'):text.index('\nif [[ "$USE_CONDA" -eq 1 ]]; then')]
    assert 'pip install ripgrep' not in fn and 'cargo' not in fn, \
        'uv path must not build ripgrep from source (cargo) — zero-compile goal'
    assert 'command -v rg' in fn, 'uv path does not detect a system rg'
    assert 'command -v fd' in fn or 'command -v fdfind' in fn, \
        'uv path does not detect a system fd/fdfind'
    _ok('uv path detects system rg/fd, never builds ripgrep from source')


def test_server_reexec_respects_uv_backend():
    """server.py must only setdefault CONDA_PREFIX when backend != 'uv'."""
    text = _server_py()
    assert "backend = cfg.get('backend')" in text, \
        "server.py re-exec does not read the marker's backend field"
    assert re.search(
        r"if backend != 'uv':\s*\n\s*os\.environ\.setdefault\('CONDA_PREFIX', env_prefix\)",
        text), 'server.py setdefaults CONDA_PREFIX unconditionally (venv would misfire)'
    _ok("server.py skips the CONDA_PREFIX shim for a uv-backed venv")


def test_bootstrap_reexec_respects_uv_backend():
    """bootstrap.py (the other marker consumer) must also skip CONDA_PREFIX /
    CONDA_DEFAULT_ENV for a uv venv, else _running_in_conda_env() misfires."""
    text = _bootstrap_py()
    assert "backend = cfg.get('backend')" in text, \
        "bootstrap.py re-exec does not read the marker's backend field"
    assert re.search(
        r"if backend != 'uv':\s*\n\s*os\.environ\.setdefault\('CONDA_PREFIX', env_prefix\)",
        text), 'bootstrap.py setdefaults CONDA_PREFIX unconditionally (venv would misfire)'
    # CONDA_DEFAULT_ENV must be nested under the same backend != 'uv' guard.
    # Asserted structurally, not as one literal line: the export logic lives in
    # _tofu_export_env_native_paths (shared with the LD_LIBRARY_PATH/fontconfig
    # exports), so the two setdefaults are no longer textually adjacent.
    guard = text.index("if backend != 'uv':")
    nxt = text.index('if not os.path.isdir(\'/etc/fonts\')', guard)
    assert re.search(r"if env_name:\s*\n\s*os\.environ\.setdefault\('CONDA_DEFAULT_ENV', env_name\)",
                     text[guard:nxt]), \
        'bootstrap.py sets CONDA_DEFAULT_ENV outside the backend != \'uv\' guard'
    _ok("bootstrap.py skips the conda shim for a uv-backed venv")


def test_playwright_env_exported_before_reexec_early_return():
    """Regression (2026-07-27): Chromium is a CHILD process and resolves libatk /
    libatk-bridge / libnss out of $env_prefix/lib, which is not on the default
    linker path. The export used to live INSIDE the re-exec branch, so a server
    launched directly with the env interpreter (the documented way) hit the
    `already_in_env` early return and never got LD_LIBRARY_PATH — every
    Playwright launch died with "libatk-1.0.so.0: cannot open shared object
    file". The export MUST happen before that early return, in both consumers.
    """
    for src, name, early_var in ((_server_py(), 'server.py', 'already_in_env'),
                                 (_bootstrap_py(), 'bootstrap.py', 'same')):
        assert '_tofu_export_env_native_paths' in src, \
            f'{name} has no native-path export helper'
        # Anchor on the CALL site (leading indent), not the bare name: the `def`
        # line always precedes the early return, so indexing the name alone
        # makes this assertion vacuously true even with the bug reintroduced.
        call = src.index('\n    _tofu_export_env_native_paths(env_prefix')
        early = src.index(f'    if {early_var}:\n        return')
        assert call < early, (
            f'{name}: the native-path export runs AFTER the {early_var} early '
            f'return — a directly launched server gets no LD_LIBRARY_PATH and '
            f'Chromium dies on libatk')
        assert re.search(r"os\.environ\['LD_LIBRARY_PATH'\]", src), \
            f'{name} never sets LD_LIBRARY_PATH'
    _ok('LD_LIBRARY_PATH is exported before the re-exec early return (both consumers)')


def test_fontconfig_exported_when_no_system_config():
    """Regression (2026-07-27): this host has no /etc/fonts, so fontconfig falls
    back to a builtin config that finds ZERO fonts. Chromium then launches and
    paints CSS backgrounds fine but draws every glyph as nothing — screenshots
    come back blank-but-styled, which reads as "the page didn't load" rather
    than as an error, so it fails silently. Both consumers must point
    fontconfig at the env's own config when the system one is absent.
    """
    for src, name in ((_server_py(), 'server.py'), (_bootstrap_py(), 'bootstrap.py')):
        assert "os.path.isdir('/etc/fonts')" in src, \
            f'{name} does not probe for a system fontconfig config'
        assert 'FONTCONFIG_PATH' in src and 'FONTCONFIG_FILE' in src, \
            f'{name} does not export FONTCONFIG_PATH/FONTCONFIG_FILE'
        # Must be conditional: a host WITH /etc/fonts keeps using it.
        assert re.search(r"if not os\.path\.isdir\('/etc/fonts'\):", src), \
            f'{name} exports fontconfig unconditionally (would override a real system config)'
    _ok('fontconfig falls back to the env config only when /etc/fonts is absent')


def test_conda_path_installs_fonts_for_chromium():
    """Chromium needs fontconfig + at least one real font family to render text.
    These were previously only present as transitive deps of other packages; a
    solver change could silently drop them and text rendering would break with
    no error. Pin them explicitly in CHROMIUM_LIBS.
    """
    text = _install_sh()
    libs = text[text.index('CHROMIUM_LIBS=('):]
    libs = libs[:libs.index(')')]
    assert 'atk-1.0' in libs, 'CHROMIUM_LIBS lost atk-1.0 (the original libatk failure)'
    assert 'fontconfig' in libs, 'CHROMIUM_LIBS does not install fontconfig'
    assert re.search(r'font-ttf-\S+', libs), \
        'CHROMIUM_LIBS installs no font family — Chromium renders text as nothing'
    _ok('conda path pins fontconfig + a real font family for Chromium')


def test_uv_path_verifies_chromium_actually_launches():
    """Downloading the browser != being able to RUN it. The uv venv has no
    conda-forge to source Chromium's GUI libs/fonts from, so on a bare host the
    binary lands but every launch dies on a missing .so. The fast path must
    prove launch+text-render at install time and print actionable recovery,
    instead of deferring the failure to a dead browser tool much later.
    """
    text = _install_sh()
    fn = text[text.index('_try_uv_install() {'):text.index('\nif [[ "$USE_CONDA" -eq 1 ]]; then')]
    assert 'chromium.launch' in fn, \
        'uv path never verifies Chromium can actually launch'
    assert 'measureText' in fn, \
        'uv path does not verify text rendering (a fontless browser screenshots blank)'
    assert 'install-deps chromium' in fn, \
        'uv path gives no root recovery command'
    assert '--use-conda' in fn, 'uv path gives no rootless recovery command'
    # The verification must never abort the install — browser tools degrade.
    assert 'fail ' not in fn and 'exit 1' not in fn, \
        'Chromium verification must not abort the install (browser tools degrade)'
    _ok('uv path verifies Chromium launches + renders text, with actionable recovery')


def test_healthcheck_probe_actually_launches_chromium():
    """`import playwright` is not evidence the browser WORKS: it stays green both
    when Chromium cannot launch (missing libatk) and when it launches with zero
    fonts (blank-but-styled screenshots). The runtime probe must really launch
    it and measure a glyph, and must self-export the env's native paths — a
    bare `python3 healthcheck.py` never ran server.py's boot exports, so
    without that it would report a FALSE failure.
    """
    with open(os.path.join(ROOT, 'healthcheck.py'), 'r', encoding='utf-8') as f:
        text = f.read()
    probe = text[text.index('# 5. Optional browser engine'):]
    probe = probe[:probe.index("print(f\"\\n{C.BOLD}")]
    assert 'chromium.launch' in probe, \
        'healthcheck browser probe never launches Chromium (import-only check)'
    assert 'measureText' in probe, \
        'healthcheck probe does not measure a glyph (misses the zero-fonts failure)'
    assert '_tofu_export_env_native_paths' in probe, \
        'healthcheck probe does not export the env native paths — false failure'
    # The two distinct failure modes must be reported distinctly, not merged.
    assert 'renders NO text' in probe, 'no distinct zero-fonts diagnosis'
    assert 'cannot launch' in probe, 'no distinct launch-failure diagnosis'
    _ok('healthcheck probe really launches Chromium + measures a glyph')


def main():
    print()
    print(_color('═══ install.sh uv fast-path / conda-fallback Guard Tests ═══', '36'))
    print()
    tests = [
        test_use_conda_flag_parses_and_defaults_off,
        test_uv_path_gated_on_glibc_and_flags,
        test_uv_fallback_is_clean_and_smoke_tests_glibc_canaries,
        test_conda_pipeline_guarded_by_fast_path_flag,
        test_conda_only_globals_preseeded_for_set_u,
        test_uv_marker_has_backend_field,
        test_uv_ripgrep_no_source_build,
        test_uv_venv_uses_managed_python,
        test_reexec_uses_prefix_not_just_executable,
        test_server_reexec_respects_uv_backend,
        test_bootstrap_reexec_respects_uv_backend,
        test_playwright_env_exported_before_reexec_early_return,
        test_fontconfig_exported_when_no_system_config,
        test_conda_path_installs_fonts_for_chromium,
        test_uv_path_verifies_chromium_actually_launches,
        test_healthcheck_probe_actually_launches_chromium,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
