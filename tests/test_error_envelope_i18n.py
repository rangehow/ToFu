"""Regression: the typed error envelope renders in the UI language.

WHY
---
``lib/error_envelope/_build.py`` historically baked BOTH languages into
``message`` / ``hint`` ("⚠️ 模型服务端点无法连接…\\nModel endpoint
unreachable…" + "解决办法 / How to fix: …"), so a zh UI and an en UI both
showed the same bilingual wall of text (reported 2026-07-25 on the
endpoint_unreachable envelope for kimi-k3).

The fix mirrors the stream-phase HUD pattern: the envelope now ALSO
carries ``titleKey`` / ``hintKey`` (``err.k.<kind>.title`` /
``err.k.<kind>.hint``); ``renderErrorEnvelope`` + ``errorEnvelopeMessage``
resolve them through the real i18n table in the CURRENT UI language.
The legacy bilingual ``message`` / ``hint`` are kept BYTE-IDENTICAL for
headless clients, old frontend bundles, and persisted pre-fix envelopes
(no keys → verbatim legacy render).

This suite locks four halves:
  1. BACKEND emission — every kind ships the right keys; custom
     message/hint overrides suppress them (unless explicitly paired).
  2. PARITY guard — the i18n table's err.k.* texts are byte-identical to
     the Python _TITLES table (drift fails the build), and chip.en ==
     ERROR_KIND_LABELS (the old English-only chip table).
  3. FRONTEND harness (real i18n.js + real error_envelope.js) — zh shows
     Chinese-only, en shows English-only, legacy/unknown-key envelopes
     fall back to the bilingual render.
  4. NEUTER — stripping the resolution line leaks the bilingual text back
     (causality proof).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest

import pytest

pytestmark = pytest.mark.unit


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _expected_legacy_message(kind: str, model: str = '') -> str:
    """Recompute the PRE-CHANGE bilingual title byte-format inline."""
    from lib.error_envelope._constants import _TITLES
    cn_title, en_title, _cn_hint, _en_hint = _TITLES[kind]
    if model:
        return f'{cn_title}（模型：{model}）\n{en_title} (model: {model})'
    return f'{cn_title}\n{en_title}'


def _expected_legacy_hint(kind: str) -> str:
    from lib.error_envelope._constants import _TITLES
    _cn_title, _en_title, cn_hint, en_hint = _TITLES[kind]
    if cn_hint and en_hint:
        return f'解决办法 / How to fix:\n{cn_hint}\n\n{en_hint}'
    return cn_hint or en_hint


# ═════════════════════════════════════════════════════════════════════
#  1. Backend emission
# ═════════════════════════════════════════════════════════════════════


class TestBackendEmitsKeys(unittest.TestCase):

    def test_every_kind_ships_title_and_hint_keys(self):
        from lib.error_envelope import KINDS, make_envelope
        self.assertGreater(len(KINDS), 15)
        for kind in sorted(KINDS):
            env = make_envelope(kind)
            self.assertEqual(env.get('titleKey'), f'err.k.{kind}.title',
                             f'{kind} missing/default titleKey')
            self.assertEqual(env.get('hintKey'), f'err.k.{kind}.hint',
                             f'{kind} missing/default hintKey')

    def test_legacy_bilingual_fields_byte_identical(self):
        """The legacy message/hint must NOT change format — headless
        clients and old bundles render them verbatim."""
        from lib.error_envelope import KINDS, make_envelope
        for kind in sorted(KINDS):
            env = make_envelope(kind)
            self.assertEqual(env['message'], _expected_legacy_message(kind),
                             f'{kind} legacy message drifted')
            self.assertEqual(env['hint'], _expected_legacy_hint(kind),
                             f'{kind} legacy hint drifted')

    def test_model_suffix_stays_in_legacy_message_only(self):
        from lib.error_envelope import make_envelope
        env = make_envelope('endpoint_unreachable', model='kimi-k3')
        self.assertEqual(env['message'],
                         _expected_legacy_message('endpoint_unreachable',
                                                  model='kimi-k3'))
        # Keyed title carries NO baked model — the frontend appends the
        # localized err.k._modelSuffix fragment.
        self.assertEqual(env['titleKey'], 'err.k.endpoint_unreachable.title')

    def test_custom_message_suppresses_title_key(self):
        from lib.error_envelope import make_envelope
        env = make_envelope('generic', message='Totally custom text')
        self.assertNotIn('titleKey', env)
        self.assertEqual(env['message'], 'Totally custom text')

    def test_custom_hint_without_key_suppresses_hint_key(self):
        from lib.error_envelope import make_envelope
        env = make_envelope('generic', hint='Custom bilingual hint')
        self.assertNotIn('hintKey', env)
        self.assertEqual(env['hint'], 'Custom bilingual hint')

    def test_custom_hint_with_explicit_key_keeps_both(self):
        from lib.error_envelope import make_envelope
        env = make_envelope('invalid_image',
                            hint='解决办法 / How to fix:\n• x\n\n• y',
                            hint_key='err.k.invalid_image.hintSize')
        self.assertEqual(env['hintKey'], 'err.k.invalid_image.hintSize')
        self.assertEqual(env['hint'], '解决办法 / How to fix:\n• x\n\n• y')

    def test_unknown_kind_downgrades_keys_to_generic(self):
        from lib.error_envelope import make_envelope
        env = make_envelope('rateLimit')  # typo — must not leak
        self.assertEqual(env['kind'], 'generic')
        self.assertEqual(env['titleKey'], 'err.k.generic.title')
        self.assertEqual(env['hintKey'], 'err.k.generic.hint')

    def test_from_exception_endpoint_unreachable_carries_keys(self):
        from lib.error_envelope import from_exception
        from lib.llm_errors import EndpointUnreachableError
        env = from_exception(EndpointUnreachableError(
            "All endpoints for model 'kimi-k3' are unreachable"),
            model='kimi-k3', context='no-fallback', source='llm-stream')
        self.assertEqual(env['kind'], 'endpoint_unreachable')
        self.assertEqual(env['titleKey'], 'err.k.endpoint_unreachable.title')
        self.assertEqual(env['hintKey'], 'err.k.endpoint_unreachable.hint')

    def test_invalid_image_call_site_pairs_hint_keys(self):
        """The two custom invalid_image hints in _call.py must each be
        paired with their explicit hint_key (static pin — driving the full
        LLM fallback path is covered elsewhere)."""
        src_path = os.path.join(ROOT, 'lib', 'tasks_pkg', 'llm_fallback',
                                '_call.py')
        with open(src_path, encoding='utf-8') as f:
            src = f.read()
        self.assertIn("_hint_key = 'err.k.invalid_image.hintMany'", src)
        self.assertIn("_hint_key = 'err.k.invalid_image.hintSize'", src)
        self.assertIn('hint_key=_hint_key,', src)


# ═════════════════════════════════════════════════════════════════════
#  Shared node plumbing (parity guard + frontend harness)
# ═════════════════════════════════════════════════════════════════════


def _node_available() -> bool:
    return bool(shutil.which('node'))


_NODE_DUMP = r"""
const fs = require('fs');
global.localStorage = { getItem: () => null, setItem: () => {} };
global.document = { addEventListener: () => {}, querySelectorAll: () => [],
                    documentElement: {} };
global.window = {};
eval(fs.readFileSync(process.argv[2], 'utf8'));   // i18n.js
/* const declarations do NOT escape eval — append the symbol read as the
 * eval's completion value so the labels table leaves the eval scope. */
const _chips = eval(fs.readFileSync(process.argv[3], 'utf8')
  + '\n;ERROR_KIND_LABELS;');
const out = { i18n: {}, chips: _chips };
for (const k of Object.keys(_i18n)) {
  if (k.startsWith('err.k.')) out.i18n[k] = _i18n[k];
}
console.log(JSON.stringify(out));
"""


def _dump_i18n_and_chips() -> dict:
    harness = os.path.join(HERE, '_err_env_dump_harness.js')
    with open(harness, 'w') as f:
        f.write(_NODE_DUMP)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'i18n.js'),
             os.path.join(JS_DIR, 'core', 'error_envelope.js')],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node dump failed: {proc.stderr}'
    return json.loads(proc.stdout.strip())


# ═════════════════════════════════════════════════════════════════════
#  2. Parity guard — i18n table vs the Python SSOT
# ═════════════════════════════════════════════════════════════════════


@unittest.skipUnless(_node_available(), 'node not installed')
class TestParityWithPythonTable(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dump = _dump_i18n_and_chips()

    def test_every_kind_has_all_three_keys(self):
        from lib.error_envelope import KINDS
        for kind in sorted(KINDS):
            for suffix in ('chip', 'title', 'hint'):
                key = f'err.k.{kind}.{suffix}'
                self.assertIn(key, self.dump['i18n'],
                              f'i18n.js missing {key}')
                entry = self.dump['i18n'][key]
                self.assertIn('zh', entry, f'{key} missing zh')
                self.assertIn('en', entry, f'{key} missing en')

    def test_title_and_hint_texts_byte_identical_to_python(self):
        """The i18n table must mirror _TITLES exactly — any drift means the
        keyed render and the legacy fallback tell different stories."""
        from lib.error_envelope._constants import _TITLES
        for kind, (cn_t, en_t, cn_h, en_h) in _TITLES.items():
            self.assertEqual(self.dump['i18n'][f'err.k.{kind}.title']['zh'], cn_t,
                             f'{kind}.title.zh drifted')
            self.assertEqual(self.dump['i18n'][f'err.k.{kind}.title']['en'], en_t,
                             f'{kind}.title.en drifted')
            self.assertEqual(self.dump['i18n'][f'err.k.{kind}.hint']['zh'], cn_h,
                             f'{kind}.hint.zh drifted')
            self.assertEqual(self.dump['i18n'][f'err.k.{kind}.hint']['en'], en_h,
                             f'{kind}.hint.en drifted')

    def test_chip_en_matches_legacy_kind_labels(self):
        """The en chip must equal the old English-only ERROR_KIND_LABELS —
        an en UI must look byte-identical to before for the chip row."""
        from lib.error_envelope import KINDS
        for kind in sorted(KINDS):
            self.assertEqual(self.dump['i18n'][f'err.k.{kind}.chip']['en'],
                             self.dump['chips'].get(kind, ''),
                             f'{kind} chip.en != ERROR_KIND_LABELS')

    def test_shared_fragments_present(self):
        self.assertEqual(self.dump['i18n']['err.k._howToFix']['zh'], '解决办法：')
        self.assertEqual(self.dump['i18n']['err.k._howToFix']['en'], 'How to fix:')
        self.assertIn('{model}', self.dump['i18n']['err.k._modelSuffix']['zh'])
        self.assertIn('{model}', self.dump['i18n']['err.k._modelSuffix']['en'])

    def test_invalid_image_variants_present(self):
        for key, zh_prefix in (
                ('err.k.invalid_image.hintMany', '• 过多大图'),
                ('err.k.invalid_image.hintSize', '• 会话中某张图片')):
            self.assertIn(key, self.dump['i18n'])
            self.assertTrue(self.dump['i18n'][key]['zh'].startswith(zh_prefix),
                            f'{key}.zh drifted')
            self.assertTrue(self.dump['i18n'][key]['en'].startswith('• '),
                            f'{key}.en missing bullet')


# ═════════════════════════════════════════════════════════════════════
#  3. Frontend harness — real i18n.js + real error_envelope.js
# ═════════════════════════════════════════════════════════════════════


_HARNESS = r"""
const fs = require('fs');
global.localStorage = { getItem: () => null, setItem: () => {} };
global.document = { addEventListener: () => {}, querySelectorAll: () => [],
                    documentElement: {} };
global.window = {};
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

const _NEUTER = process.argv[4] === 'neuter-resolve';
let _envSrc = fs.readFileSync(process.argv[3], 'utf8');
if (_NEUTER) {
  const _target = 'return text;  // [env-i18n-resolve]';
  if (!_envSrc.includes(_target)) throw new Error('neuter target missing');
  _envSrc = _envSrc.replace(_target, 'return null;  // NEUTERED');
}
eval(fs.readFileSync(process.argv[2], 'utf8'));   // i18n.js (zh default)
eval(_envSrc);                                    // error_envelope.js

const out = [];
function check(name, cond, note) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : (' — ' + (note || ''))));
}

function _keyedEnv() {
  // Mirrors lib.error_envelope.make_envelope('endpoint_unreachable',
  // model='kimi-k3', context='no-fallback') post-fix.
  return {
    kind: 'endpoint_unreachable', severity: 'warning', retryable: true,
    message: '⚠️ 模型服务端点无法连接（服务可能已宕机）（模型：kimi-k3）\n'
           + 'Model endpoint unreachable (server may be down) (model: kimi-k3)',
    hint: '解决办法 / How to fix:\n• 模型服务端点无法连接（连接被拒绝或超时），通常说明该自建/BYO 服务已宕机、端口未监听，或网络/防火墙不通。\n• 确认模型服务正在运行且可达后重试；或在 「设置 → 模型默认」 切换到其他可用模型。\n\n'
        + '• The model endpoint refused the connection or timed out — the self-hosted / BYO server is likely down, the port is not listening, or a firewall is blocking it.\n'
        + '• Verify the model server is running and reachable, then retry; or switch to another available model in "Settings → Model defaults".',
    detail: "All endpoints for model 'kimi-k3' are unreachable",
    model: 'kimi-k3', context: 'no-fallback', source: 'llm-stream',
    raw: '', titleKey: 'err.k.endpoint_unreachable.title',
    hintKey: 'err.k.endpoint_unreachable.hint',
  };
}

// ── A. zh UI (default) → Chinese-only block ──
// (normal probes are skipped in NEUTER mode: with the resolution seam
//  stripped they are red BY DESIGN and would pollute the no-FAIL assert)
if (!_NEUTER) {
  const html = renderErrorEnvelope(_keyedEnv());
  check('zh_title_localized',
    html.includes('⚠️ 模型服务端点无法连接（服务可能已宕机）（模型：kimi-k3）'), html);
  check('zh_english_title_not_shown',
    !html.includes('Model endpoint unreachable (server may be down)'), html);
  check('zh_hint_header_localized',
    html.includes('解决办法：') && !html.includes('How to fix'), html);
  check('zh_english_hint_not_shown',
    !html.includes('The model endpoint refused the connection'), html);
  check('zh_chip_localized',
    html.includes('端点不可达'), html);
  check('zh_context_tag_preserved', html.includes('[no-fallback]'), html);
}

// ── B. en UI → English-only block ──
if (!_NEUTER) {
  _i18nLang = 'en';
  const html = renderErrorEnvelope(_keyedEnv());
  check('en_title_localized',
    html.includes('Model endpoint unreachable (server may be down) (model: kimi-k3)'), html);
  check('en_zh_title_not_shown', !html.includes('模型服务端点无法连接（服务可能已宕机）'), html);
  check('en_hint_header_localized',
    html.includes('How to fix:') && !html.includes('解决办法'), html);
  check('en_chip_english', html.includes('Endpoint unreachable'), html);
  _i18nLang = 'zh';
}

// ── C. LEGACY envelope (pre-fix persisted shape: no keys) → bilingual
//       render byte-identical to before ──
if (!_NEUTER) {
  const legacy = _keyedEnv();
  delete legacy.titleKey;
  delete legacy.hintKey;
  const html = renderErrorEnvelope(legacy);
  check('legacy_bilingual_title_shown',
    html.includes('模型服务端点无法连接（服务可能已宕机）')
      && html.includes('Model endpoint unreachable (server may be down)'), html);
  check('legacy_bilingual_hint_shown',
    html.includes('解决办法 / How to fix:')
      && html.includes('The model endpoint refused the connection'), html);
  // Chip still localizes (it comes from the i18n table, not the envelope).
  check('legacy_chip_still_localized', html.includes('端点不可达'), html);
}

// ── D. UNKNOWN key (table drift / newer backend) → legacy fallback ──
if (!_NEUTER) {
  const drift = _keyedEnv();
  drift.titleKey = 'err.k.endpoint_unreachable.title.V2';
  drift.hintKey = 'err.k.endpoint_unreachable.hint.V2';
  const html = renderErrorEnvelope(drift);
  check('unknown_key_falls_back_to_bilingual',
    html.includes('解决办法 / How to fix:')
      && html.includes('Model endpoint unreachable (server may be down)'), html);
}

// ── E. aborted kind: hint key resolves to EMPTY → no hint block at all ──
if (!_NEUTER) {
  const aborted = {
    kind: 'aborted', severity: 'warning', retryable: false,
    message: '⏹️ 用户已中止\nStopped by user', hint: '',
    detail: '', model: '', context: '', source: '', raw: '',
    titleKey: 'err.k.aborted.title', hintKey: 'err.k.aborted.hint',
  };
  const html = renderErrorEnvelope(aborted);
  check('aborted_no_hint_block',
    !html.includes('解决办法') && !html.includes('How to fix'), html);
  check('aborted_title_zh', html.includes('⏹️ 用户已中止'), html);
  check('aborted_en_title_not_shown', !html.includes('Stopped by user'), html);
}

// ── F. errorEnvelopeMessage (paper/translation consumers) → localized ──
if (!_NEUTER) {
  const msg = errorEnvelopeMessage(_keyedEnv());
  check('message_helper_localized',
    msg === '⚠️ 模型服务端点无法连接（服务可能已宕机）（模型：kimi-k3）', msg);
  const legacy = _keyedEnv();
  delete legacy.titleKey;
  check('message_helper_legacy_fallback',
    errorEnvelopeMessage(legacy).includes('\n'), errorEnvelopeMessage(legacy));
}

// ── NEUTER: resolution stripped → bilingual text leaks back ──
if (_NEUTER) {
  const html = renderErrorEnvelope(_keyedEnv());
  check('NEUTER_bilingual_leaks_back',
    html.includes('Model endpoint unreachable (server may be down)')
      && html.includes('解决办法 / How to fix:'), html);
}

console.log(out.join('\n'));
"""


def _run_harness(neuter: bool = False) -> str:
    harness = os.path.join(HERE, '_err_env_i18n_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'i18n.js'),
             os.path.join(JS_DIR, 'core', 'error_envelope.js'),
             'neuter-resolve' if neuter else ''],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'error-envelope i18n failures:\n' + output
    return output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_error_envelope_frontend_localizes():
    output = _run_harness()
    assert output.count('PASS') >= 19, f'expected >=19 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_error_envelope_neuter_resolution():
    """NEUTER proof: stripping the keyed-resolution line makes the legacy
    bilingual text leak back into the block — the helpers under test are
    what localize the envelope."""
    output = _run_harness(neuter=True)
    assert 'PASS NEUTER_bilingual_leaks_back' in output, output


# ═════════════════════════════════════════════════════════════════════
#  4. Static pins — the neuter target + bundle registration
# ═════════════════════════════════════════════════════════════════════


class TestStaticPins(unittest.TestCase):

    def test_resolution_line_marker_present(self):
        """The NEUTER target line must exist exactly once (it is the single
        seam the harness strips)."""
        with open(os.path.join(JS_DIR, 'core', 'error_envelope.js'),
                  encoding='utf-8') as f:
            src = f.read()
        self.assertEqual(
            src.count('return text;  // [env-i18n-resolve]'), 1)

    def test_error_envelope_in_bundle(self):
        """error_envelope.js must be in _BUNDLE_FILES, before main.js."""
        from lib.js_bundler import _BUNDLE_FILES
        self.assertIn('core/error_envelope.js', _BUNDLE_FILES)
        self.assertLess(_BUNDLE_FILES.index('i18n.js'),
                        _BUNDLE_FILES.index('core/error_envelope.js'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
