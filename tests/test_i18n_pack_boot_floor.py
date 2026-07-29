#!/usr/bin/env python3
"""Guards for the i18n PACK-MODE boot floor — the "absent pack" failure class.

WHY THIS EXISTS (owner-identified gap, 2026-07-29)
--------------------------------------------------
4b3398bf fixed how a STALE pack resolves. It did nothing for a pack that is
MISSING — 404, truncated by a proxy, evicted, or lost to a future cleanup bug.
That is a different path to the same catastrophe, because pack mode
deliberately removes i18n.js from the core bundle
(``lib/js_bundler.build_bundle``): the pack becomes the ONLY copy of
``_i18n`` / ``_i18nLang`` / ``t()``, i.e. a single point of failure for the
entire UI.

Three defects made "pack absent" unsurvivable AND invisible:

  1. NO BOOT FLOOR FOR ``_i18nLang``. index.html has stubbed ``t()`` since
     forever, but never the symbol beside it. Any BARE read therefore threw
     mid-boot. Reproduced under node before the fix:

         window.t = function(key){ return key; };   // the only fallback
         finish_info.js:189  -> ReferenceError: _i18nLang is not defined
         translation.js:62   -> OK (guarded), code=zh

     That ReferenceError is verbatim what production logged.

  2. THE CAPABILITY CHECK WAS BLIND BY CONSTRUCTION. ``_loadBearingCaps``
     asserts ``typeof window['t'] === 'function'`` — which can NEVER fail,
     because the boot block stubs ``window.t``. A totally absent dictionary
     looked healthy. The assertion has to be on the DICTIONARY, which is what
     the pack actually carries.

  3. PACK LOAD FAILURE WAS SILENT. The tag used the generic
     ``_onScriptError``, which only records + banners after other checks pass;
     nothing retried, so a single lost 212KB file left the app rendering raw
     keys forever.

THE STRUCTURAL RULE THESE PIN
-----------------------------
When the core bundle EXCLUDES a module, every symbol that module owns and the
bundle references BARE must have a boot-level floor. Test 1 below enforces it
by scanning the real manifest, so a fourth bare ``_i18nLang`` added next month
goes red instead of shipping.

Run: python3 tests/test_i18n_pack_boot_floor.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest
except ImportError:
    pytest = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, 'static', 'js')
INDEX = os.path.join(ROOT, 'index.html')


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


def _read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


# Symbols DEFINED BY i18n.js, which pack mode removes from the core bundle.
# Each is therefore absent whenever the pack fails to load.
_PACK_OWNED_SYMBOLS = ('_i18nLang', '_i18n')


def _strip_comments(src: str) -> str:
    """Blank out block + line comments, PRESERVING line numbering.

    Prose mentioning the symbol ("Maps _i18nLang to the language NAME…") is
    not a runtime read; counting it produced false positives that would have
    forced pointless edits to documentation.
    """
    out = []
    i, n = 0, len(src)
    in_block = in_line = False
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ''
        if in_block:
            if c == '*' and nxt == '/':
                in_block = False
                out.append('  ')
                i += 2
                continue
            out.append('\n' if c == '\n' else ' ')
        elif in_line:
            if c == '\n':
                in_line = False
                out.append('\n')
            else:
                out.append(' ')
        elif c == '/' and nxt == '*':
            in_block = True
            out.append('  ')
            i += 2
            continue
        elif c == '/' and nxt == '/':
            in_line = True
            out.append('  ')
            i += 2
            continue
        else:
            out.append(c)
        i += 1
    return ''.join(out)


def _enclosing_span(lines, idx):
    """Return the (start, end) line span of the top-level function containing
    line ``idx``, or a small window when the reference is not inside one.

    Guards are frequently on a DIFFERENT line from the read — an early
    ``if (typeof _i18n === 'undefined') return null;`` protects every later
    line in the same function, and a ternary's ``typeof`` may sit one line
    above its use. A line-local check calls both of those violations, which is
    wrong: the runtime scope is the function, so that is what we examine.
    """
    start = 0
    for j in range(idx, -1, -1):
        if re.match(r'^(async\s+)?function\s+\w+\s*\(', lines[j]):
            start = j
            break
    else:
        return max(0, idx - 3), min(len(lines), idx + 2)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j] == '}':
            end = j + 1
            break
    return start, end


def _bare_references(src: str, symbol: str):
    """Yield (lineno, line) for BARE reads of *symbol* — no typeof guard.

    A guarded read (``typeof _i18nLang !== 'undefined'``) is safe when the
    symbol is absent; a bare read throws. Declarations, property accesses
    (``window._i18nLang``) and comments are not bare reads of the global, and
    a guard anywhere in the enclosing function counts as protection.
    """
    code_src = _strip_comments(src)
    lines = code_src.splitlines()
    raw_lines = src.splitlines()
    pat = re.compile(r'(?<![\w.$])' + re.escape(symbol) + r'\b')
    for i, code in enumerate(lines):
        if not pat.search(code):
            continue
        if re.match(r'^\s*(var|let|const)\s+' + re.escape(symbol) + r'\b', code):
            continue
        lo, hi = _enclosing_span(lines, i)
        scope = '\n'.join(lines[lo:hi])
        if re.search(r'typeof\s+' + re.escape(symbol) + r'\b', scope):
            continue
        yield i + 1, raw_lines[i].strip()


@_unit
def test_no_core_bundle_file_bare_references_a_pack_owned_symbol():
    """THE STRUCTURAL RULE. Scans the REAL manifest, not a hand-copied list.

    ui/finish_info.js:189 was the violation that reached production; without
    this guard the next one ships the same way.
    """
    from lib.js_bundler import _BUNDLE_FILES
    violations = []
    for name in _BUNDLE_FILES:
        if name == 'i18n.js':
            continue  # the definition itself; excluded from the bundle in pack mode
        path = os.path.join(JS_DIR, name)
        if not os.path.exists(path):
            continue
        src = _read(path)
        for sym in _PACK_OWNED_SYMBOLS:
            for lineno, line in _bare_references(src, sym):
                violations.append(f'{name}:{lineno}  {sym}  |  {line[:90]}')
    assert not violations, (
        'core-bundle file(s) BARE-reference a symbol that only the i18n pack '
        'defines. In pack mode the bundle excludes i18n.js, so a failed pack '
        'load makes each of these throw ReferenceError mid-boot and the app '
        'never finishes initializing:\n  ' + '\n  '.join(violations)
        + '\nFix: read via `typeof X !== \'undefined\' ? X : <default>`.')


@_unit
def test_index_html_declares_a_boot_floor_for_i18n_lang():
    """t() has had a floor forever; the symbol beside it must too."""
    html = _read(INDEX)
    assert "typeof _i18nLang === 'undefined'" in html, (
        'index.html has no boot floor for _i18nLang. The pack is its only '
        'other definition, so without this a lost pack throws on first read.')
    assert 'tofu_ui_lang' in html, (
        'the floor must seed from localStorage tofu_ui_lang so the language '
        'the user chose survives a pack failure')


@_unit
def test_boot_floor_actually_prevents_the_production_referenceerror():
    """Behavioural, not textual: run the real guarded code with NO pack."""
    if not shutil.which('node'):
        print('SKIP (node unavailable)')
        return
    html = _read(INDEX)
    m = re.search(r"if \(typeof _i18nLang === 'undefined'\) \{.*?\n  \}",
                  html, re.S)
    assert m, 'boot-floor block not found in index.html'
    floor = m.group(0)

    fi = _read(os.path.join(JS_DIR, 'ui', 'finish_info.js'))
    fn = re.search(r'function _translateCacheCause\(s\) \{.*?\n\}', fi, re.S)
    assert fn, '_translateCacheCause not found'

    script = (
        'global.window = global;\n'
        'global.localStorage = { getItem: function(){ return "zh"; } };\n'
        + floor + '\n'
        'const _CACHE_CAUSE_PHRASES = [];\n'
        + fn.group(0) + '\n'
        'console.log("RESULT:" + _translateCacheCause("prefix not reused"));\n'
    )
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(script)
        path = fh.name
    try:
        r = subprocess.run([shutil.which('node'), path], capture_output=True,
                           text=True, timeout=60)
    finally:
        os.unlink(path)
    assert r.returncode == 0, (
        'the pack-absent path STILL throws — this is the production failure:\n'
        + (r.stderr or '')[:400])
    assert 'RESULT:' in r.stdout, r.stdout[:300]


@_unit
def test_pack_tag_uses_the_self_healing_error_handler():
    """A lost pack must retry, not fall through the generic recorder."""
    import lib.js_bundler as jb
    saved = (jb._pack_filenames, jb._bundle_includes_i18n)
    try:
        jb._pack_filenames = {'zh': 'i18n-zh-4fe3959f.js',
                              'en': 'i18n-en-db74e770.js'}
        jb._bundle_includes_i18n = False
        tag = jb.get_i18n_pack_tag('zh')
        assert tag, 'no pack tag emitted while packs are active'
        assert '_onI18nPackError' in tag, (
            f'pack tag still uses the generic handler: {tag}. The pack is the '
            f'only copy of the dictionary — its failure needs a retry and an '
            f'explicit banner, not a silent raw-key render.')
    finally:
        jb._pack_filenames, jb._bundle_includes_i18n = saved


@_unit
def test_index_html_defines_that_handler():
    """The tag references it by name — a missing definition is a dead onerror."""
    html = _read(INDEX)
    assert 'function _onI18nPackError(' in html, (
        'js_bundler emits onerror="_onI18nPackError(event)" but index.html '
        'never defines it — the handler would silently do nothing')
    m = re.search(r'function _onI18nPackError\(e\) \{.*?\n  \}', html, re.S)
    assert m and 'retry' in m.group(0).lower(), (
        'the handler must RETRY the pack; a stale hash now 302s to the '
        'current one, so one re-request recovers the common case')


@_unit
def test_capability_check_asserts_the_dictionary_not_just_t():
    """The blindness that let a missing pack look healthy.

    ``typeof window.t === 'function'`` can never fail — the boot block stubs
    window.t. The assertion has to be on the dictionary the pack carries.
    """
    html = _read(INDEX)
    m = re.search(r'function _capabilityCheck\(\) \{.*?\n\}', html, re.S)
    assert m, '_capabilityCheck not found'
    body = m.group(0)
    assert '_i18n' in body and 'Object.keys' in body, (
        '_capabilityCheck does not assert the i18n DICTIONARY. Because '
        'window.t is stubbed at boot, a typeof-t check passes even when the '
        'entire dictionary is absent — which is exactly how a missing pack '
        'reached users as a silent wall of raw keys.')


@_unit
def test_stubbed_t_really_does_mask_the_absence():
    """Pin the PREMISE of the test above, so it can't rot into a tautology."""
    html = _read(INDEX)
    assert 'window.t = function(key) { return key; };' in html, (
        'the t() stub changed shape — re-verify whether _capabilityCheck can '
        'now detect a missing dictionary via typeof t, and re-anchor the '
        'dictionary assertion accordingly')


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
