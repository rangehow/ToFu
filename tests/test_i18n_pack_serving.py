#!/usr/bin/env python3
"""Serving-level guards for the i18n single-language split (slice 2).

WHAT THIS PROVES, END TO END
----------------------------
Epic-E sub-part 1's final slice changes WHAT the server sends: the core
bundle excludes i18n.js, and the server injects a per-language pack tag in
its place. The failure modes are all invisible in production:

  * pack tag AFTER the bundle tag  → t() undefined at bundle exec (both are
    defer; document order decides). Boot breaks for EVERY user, no error
    that points at i18n.
  * bundle excludes i18n.js but no pack tag → the index.html stub
    ``t = key => key`` survives → the entire UI renders raw keys.
  * setLanguage switch with a broken merge → the boot language's entries
    vanish; half the UI flips to raw keys / fallback.
  * emission failure served as split → same as "no pack tag".

These tests drive the REAL serving path (flask_client → index_page) and the
REAL generated artifacts.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_i18n_pack_serving.py
"""

import json
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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(REPO, 'static', 'js')

pytestmark = pytest.mark.unit if pytest else None


def _have_node():
    return shutil.which('node') is not None


# ── Served-HTML composition (the boot-order hard constraint) ─────────────

def _get_index(flask_client, cookie=None):
    headers = {'Cookie': f'tofu_ui_lang={cookie}'} if cookie else {}
    resp = flask_client.get('/', headers=headers)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_zh_boot_injects_pack_tag_BEFORE_bundle_tag(flask_client):
    """THE hard constraint: t() must be defined before the bundle executes.

    Both scripts are defer → document order decides. A reversed order leaves
    t() undefined at bundle exec time and the index.html stub (t = key) wins
    → the whole UI renders raw keys. This is the failure the entire boot-sync
    design exists to prevent.
    """
    html = _get_index(flask_client, 'zh')
    m_pack = re.search(r'static/js/(i18n-zh-[0-9a-f]{8}\.js)', html)
    m_bundle = re.search(r'static/js/(bundle-[0-9a-f]{8}\.js)', html)
    assert m_pack, 'no zh pack tag in served HTML — is the split active?'
    assert m_bundle, 'no bundle tag in served HTML'
    assert m_pack.start() < m_bundle.start(), (
        'pack tag appears AFTER the bundle tag — t() is undefined when the '
        'bundle executes (boot order violation)')


def test_en_cookie_gets_the_en_pack(flask_client):
    html = _get_index(flask_client, 'en')
    assert re.search(r'<script defer src="static/js/i18n-en-[0-9a-f]{8}\.js"', html), (
        'an en client was NOT served the en pack')
    # The __I18N_PACK_URLS__ map legitimately names BOTH packs (setLanguage
    # needs the other one); the negative must apply to the SCRIPT TAG only.
    assert not re.search(r'<script defer src="static/js/i18n-zh-[0-9a-f]{8}\.js"', html), (
        'an en client was served BOTH pack script tags — the per-language '
        'split is leaky')


def test_missing_and_hostile_cookies_get_the_default_pack(flask_client):
    for cookie in (None, 'fr', '../../etc/passwd', 'zh-CN'):
        html = _get_index(flask_client, cookie)
        assert re.search(r'static/js/i18n-zh-[0-9a-f]{8}\.js', html), (
            f'cookie={cookie!r} did not fall back to the zh pack')


def test_pack_urls_injected_for_setlanguage_fetch(flask_client):
    html = _get_index(flask_client, 'zh')
    m = re.search(r'window\.__I18N_PACK_URLS__=(\{[^}]+\})', html)
    assert m, ('__I18N_PACK_URLS__ missing — setLanguage cannot fetch the '
               'other language on demand')
    urls = json.loads(m.group(1))
    assert set(urls) == {'zh', 'en'}
    for lang, url in urls.items():
        path = os.path.join(REPO, url)
        assert os.path.exists(path), (
            f'injected {lang} pack URL {url} does not exist on disk — '
            f'setLanguage would 404')


# ── Artifact content: bundle excludes, pack carries ──────────────────────

def test_served_bundle_excludes_dictionary_and_pack_carries_it(flask_client):
    html = _get_index(flask_client, 'zh')
    bundle_name = re.search(r'static/js/(bundle-[0-9a-f]{8}\.js)', html).group(1)
    pack_name = re.search(r'static/js/(i18n-zh-[0-9a-f]{8}\.js)', html).group(1)
    bundle = open(os.path.join(JS_DIR, bundle_name), encoding='utf-8').read()
    pack = open(os.path.join(JS_DIR, pack_name), encoding='utf-8').read()

    # The bundle is esbuild-minified — `var _i18n = {` never appears in its
    # spaced form either way, so assert on a DICTIONARY KEY (string literals
    # survive minification) instead.
    assert 'sidebar.settings' not in bundle, (
        'the served bundle still contains the dictionary — the split is not '
        'actually saving anything (or pack state and bundle shape drifted)')
    assert 'var _i18n = {' in pack, 'the pack lost the dictionary'
    assert 'function t(' in pack, (
        'the pack lost t() — the bundle no longer has a copy, so this blanks '
        'translation app-wide')
    assert 'function setLanguage(' in pack
    assert '_reportMissingTranslation' in pack


def test_each_pack_is_single_language(flask_client):
    """The zh pack must not contain en text and vice versa (sampled)."""
    import lib.js_bundler as B
    for lang, other in (('zh', 'en'), ('en', 'zh')):
        name = B._pack_filenames.get(lang)
        assert name, f'no {lang} pack in bundler state'
        text = open(os.path.join(JS_DIR, name), encoding='utf-8').read()
        # Entry shape in the pack is "key":{zh:"…"} — the lang is an
        # UNQUOTED identifier (valid JS object-literal key). Accessor forms
        # (entry.zh / entry[_i18nLang]) use a dot/bracket, not a brace, so
        # they cannot false-match these patterns.
        assert '{' + lang + ':' in text, f'{lang} pack has no {lang} entries'
        assert '{' + other + ':' not in text, (
            f'{lang} pack still carries {other} entries — the split saves nothing')


# ── The setLanguage merge (node, real packs) ─────────────────────────────

def test_setlanguage_fetch_merge_keeps_both_languages():
    """Boot zh → switch en: the en pack merges in, zh entries SURVIVE.

    A merge that clobbers _i18n (instead of unioning) deletes the boot
    language — switching back, or any code holding zh strings, then renders
    fallback/raw keys.
    """
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    import lib.js_bundler as B
    zh_pack = os.path.join(JS_DIR, B._pack_filenames['zh'])
    en_pack = os.path.join(JS_DIR, B._pack_filenames['en'])

    harness = f"""
globalThis.window = globalThis;
globalThis.localStorage = {{ getItem: () => 'zh', setItem: () => {{}} }};
globalThis.document = {{
  documentElement: {{}}, querySelectorAll: () => [],
  addEventListener: () => {{}}, readyState: 'complete',
  get cookie() {{ return ''; }}, set cookie(v) {{}},
}};
window.__I18N_PACK_URLS__ = {{ en: {json.dumps('file://' + en_pack)} }};
const fs = require('fs');
eval(fs.readFileSync({json.dumps(zh_pack)}, 'utf8'));

const before_zh = t('sidebar.settings');
const isZh = before_zh === '设置';

// Simulate _i18nFetchPack('en') with a direct eval of the pack file (the
// script-tag plumbing is 8 lines; the LOAD-BEARING part is the merge).
(async () => {{
  if (!_i18nHasLang('en')) {{
    const prevDict = _i18n;
    eval(fs.readFileSync({json.dumps(en_pack)}, 'utf8'));
    const packDict = _i18n;
    _i18n = prevDict;
    _i18nMergeDict(packDict);
  }}
  _i18nLang = 'en';
  const after_en = t('sidebar.settings');
  const zh_still = (function() {{
    const saved = _i18nLang; _i18nLang = 'zh';
    const v = t('sidebar.settings'); _i18nLang = saved; return v;
  }})();
  console.log('@@' + JSON.stringify({{
    before_zh, isZh, after_en, zh_still,
    hasEn: _i18nHasLang('en'),
  }}));
}})();
"""
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(harness)
        path = fh.name
    try:
        r = subprocess.run([shutil.which('node'), path],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, f'node failed: {r.stderr[:600]}'
        out = [l for l in r.stdout.splitlines() if l.startswith('@@')][-1]
        res = json.loads(out[2:])
        assert res['isZh'], f"zh boot gave {res['before_zh']!r}, expected 设置"
        assert res['after_en'] == 'Settings', (
            f"after merging the en pack, t() gave {res['after_en']!r} — "
            f"the fetch+merge did not deliver English")
        assert res['zh_still'] == '设置', (
            'zh entries were CLOBBERED by the merge — switching back would '
            'render fallback/raw keys')
        assert res['hasEn']
    finally:
        os.unlink(path)


# ── The dual fallback (emission failure must not blank t()) ──────────────

def test_failed_emission_falls_back_to_dual_bundle(flask_client):
    """Kill emission → the bundle must CONTAIN i18n.js and NO pack tag.

    The split-without-pack shape (bundle excludes i18n.js, no pack served) is
    the one configuration that blanks t() app-wide. The fallback must make it
    unreachable even when the pack pipeline breaks.
    """
    import lib.js_bundler as B
    import lib.i18n_packs as P

    original = P.emit_pack_files
    def _boom(*a, **k):
        raise P.PackExtractionError('synthetic failure for the fallback test')
    try:
        P.emit_pack_files = _boom
        name = B.build_bundle()
        assert name, 'bundle build failed outright on emission failure'
        assert B._bundle_includes_i18n, (
            'bundle was built WITHOUT i18n.js while packs were unavailable — '
            'this is the split-without-pack shape that blanks t()')
        assert not B._pack_filenames
        content = open(os.path.join(JS_DIR, name), encoding='utf-8').read()
        # Minified bundle: assert on a dictionary KEY, not spaced syntax.
        assert 'sidebar.settings' in content, (
            'fallback bundle does not contain the dictionary')

        html = _get_index(flask_client, 'zh')
        assert 'i18n-zh-' not in html and 'i18n-en-' not in html, (
            'pack tags still injected while the bundle carries i18n.js — '
            'double-dictionary (wasteful) or version skew (dangerous)')
        assert '__I18N_PACK_URLS__' not in html
    finally:
        P.emit_pack_files = original
        # Restore the split state for whatever runs next in this session.
        B.build_bundle()


def test_split_state_restored_after_fallback_test(flask_client):
    """The suite must leave the world as it found it (split active)."""
    import lib.js_bundler as B
    if B._bundle_includes_i18n:
        B.build_bundle()
    assert not B._bundle_includes_i18n, (
        'split state was not restored — subsequent tests would see the dual '
        'fallback and pass/fail for the wrong reason')
    html = _get_index(flask_client, 'zh')
    assert re.search(r'static/js/i18n-zh-[0-9a-f]{8}\.js', html)


# ── Acceptance (a): the saving is real on the SERVED artifacts ───────────

def test_served_split_is_materially_smaller_than_dual():
    """Acceptance criterion (a) from the epic: measure the SERVED shape.

    dual: bundle-with-i18n.js. split: bundle-without + zh pack. Compress both
    with the codec the server actually ships (brotli q9 for immutable assets).
    """
    try:
        import brotli
    except ImportError:
        print('SKIP (brotli unavailable)')
        return
    import lib.js_bundler as B

    bundle_name = B._bundle_filename
    split_bundle = open(os.path.join(JS_DIR, bundle_name), 'rb').read()
    zh_pack = open(os.path.join(JS_DIR, B._pack_filenames['zh']), 'rb').read()
    dual_i18n = open(os.path.join(JS_DIR, 'i18n.js'), 'rb').read()

    # What a zh client downloads in each world (bundle + i18n code):
    split_bytes = len(brotli.compress(split_bundle, quality=9)) + \
        len(brotli.compress(zh_pack, quality=9))
    # Dual world: same bundle but WITH i18n.js — approximate by adding the
    # dual file's compressed size (the bundle is per-file minified then
    # concatenated, so the delta is the i18n.js contribution either way).
    dual_bytes = len(brotli.compress(split_bundle, quality=9)) + \
        len(brotli.compress(dual_i18n, quality=9))

    saved = (dual_bytes - split_bytes) / 1024
    assert saved > 25.0, (
        f'split saves only {saved:.1f} KB compressed — measured 37.9 KB on '
        f'2026-07-26; a large drop means the dictionary or generator changed')
    print(f'served split saves {saved:.1f} KB compressed '
          f'({dual_bytes/1024:.1f} -> {split_bytes/1024:.1f} KB)')
