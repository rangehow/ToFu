#!/usr/bin/env python3
"""tests/test_probe_nonchat_skip.py — access-matrix probe must NOT chat-probe
non-chat models (image_gen / embedding / transcription).

WHY
---
The matrix's "Probe & Recommend" sends a tiny ``/chat/completions`` request
(``max_tokens: 1``) to every (key × model-id) cell. For models with no chat
surface the gateway has NO chat binding to route to — the Meituan AIGC
gateway (a Java service) then picks a random upstream from an EMPTY candidate
list: ``Random.nextInt(0)`` → ``IllegalArgumentException: bound must be
positive`` → HTTP 500. Confirmed against the live persisted snapshot
(data/config/probe_cache): every ``bound must be positive`` 500 belonged to
an image/embedding model (gemini-3-pro-image-preview, gemini-2.5-flash-image,
gemini-3.1-flash-image-preview, text-embedding-v4) while every chat model got
a real verdict. The probe then flagged those cells ``unavailable`` with
``recommend_disable=True`` — one click on "Apply recommendations" would have
disabled WORKING image models.

THE FIX (three seams, all asserted here)
----------------------------------------
  * BACKEND — ``lib/provider_probe.run_cell_probe_task`` skips cells whose
    capabilities fail ``is_chat_model`` (zero network), verdict ``skipped``,
    ``recommend_disable=False``, counted separately in ``summary.skipped``.
  * ROUTE — ``routes/config.probe_provider_cells_start`` carries each model's
    ``capabilities`` into the work tuples so the backend can skip.
  * FRONTEND — ``access_matrix.js`` sends capabilities, renders the
    ``skipped`` pip, reconciles STALE persisted snapshots (downgrades
    non-chat false positives so old disk snapshots heal without a retest),
    and refuses to apply a disable recommendation to a non-chat model.

Layers (A)-(D) are backend; (E)-(G) run the REAL shipped access_matrix.js
under node. (C)/(G) are NEUTER proofs that the guards are load-bearing.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = pytest.mark.unit

import lib.provider_probe as pp  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
ACCESS_MATRIX_JS = os.path.join(ROOT, 'static', 'js', 'settings', 'providers', 'access_matrix.js')
MODEL_CAPS_JS = os.path.join(ROOT, 'static', 'js', 'core', 'model_caps.js')

_NONCHAT_CAPS = (['image_gen'], ['embedding'], ['transcription'])


def _task() -> dict:
    return {
        'provider_id': 't-nonchat',
        'status': 'running',
        'started_at': 0, 'finished_at': None,
        'total': 0, 'done_count': 0,
        'cells': {}, 'summary': {'ok': 0, 'disable': 0},
        'error': None, 'attempts': 1, '_abort': False,
        '_base_url': 'https://gw.example.com/v1',
        '_extra_headers': {},
    }


class _ProbeRedirect(unittest.TestCase):
    """Redirect the probe cache into a temp dir for every test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = pp.probe_cache_path
        pp.probe_cache_path = lambda pid: os.path.join(self._tmp.name, pid + '.json')

    def tearDown(self):
        pp.probe_cache_path = self._orig
        self._tmp.cleanup()


class BackendSkipTest(_ProbeRedirect):

    def test_nonchat_cells_skipped_without_network(self):
        """image_gen / embedding / transcription cells must be verdicted
        'skipped' with recommend_disable=False and ZERO network calls;
        the chat cell is probed normally; summary counts skipped apart."""
        probed = []

        def fake_probe(base_url, api_key, model_id, extra_headers, timeout, protocol='openai'):
            probed.append(model_id)
            return 'ok', 'HTTP 200'

        work = [
            (0, 'sk-a', 'chat-x', 'chat-x', ['text']),
            (0, 'sk-a', 'img-1', 'img-1', ['image_gen']),
            (0, 'sk-a', 'img-1', 'img-1-alias', ['image_gen']),  # alias inherits caps
            (0, 'sk-a', 'emb-1', 'emb-1', ['embedding']),
            (0, 'sk-a', 'asr-1', 'asr-1', ['transcription']),
        ]
        task = _task()
        task['total'] = len(work)
        with mock.patch.object(pp, 'probe_one_cell', side_effect=fake_probe):
            pp.run_cell_probe_task(task, work, timeout=5)

        self.assertEqual(probed, ['chat-x'], 'only the chat model may be probed')
        for mid in ('img-1', 'img-1-alias', 'emb-1', 'asr-1'):
            cell = task['cells'][pp.probe_cell_key(0, mid)]
            self.assertEqual(cell['status'], 'skipped', mid)
            self.assertFalse(cell['recommend_disable'], mid)
            self.assertIn('non-chat model', cell['detail'], mid)
        self.assertEqual(task['cells'][pp.probe_cell_key(0, 'chat-x')]['status'], 'ok')
        self.assertEqual(task['summary'],
                         {'ok': 1, 'disable': 0, 'skipped': 4},
                         'skipped cells must NOT inflate the ok count')

    def test_chat_caps_and_legacy_tuples_still_probed(self):
        """caps=['text'], caps=['audio_chat'], caps=[] and legacy 4-tuples
        (no caps element at all) are all chat → probed as before."""
        probed = []

        def fake_probe(base_url, api_key, model_id, extra_headers, timeout, protocol='openai'):
            probed.append(model_id)
            return 'ok', 'HTTP 200'

        work = [
            (0, 'sk-a', 'm-text', 'm-text', ['text']),
            (0, 'sk-a', 'm-audio', 'm-audio', ['audio_chat']),
            (0, 'sk-a', 'm-omni', 'm-omni', ['text', 'audio_chat', 'vision']),
            (0, 'sk-a', 'm-empty', 'm-empty', []),
            (0, 'sk-a', 'm-legacy', 'm-legacy'),               # 4-tuple, no caps
        ]
        task = _task()
        task['total'] = len(work)
        with mock.patch.object(pp, 'probe_one_cell', side_effect=fake_probe):
            pp.run_cell_probe_task(task, work, timeout=5)

        self.assertEqual(sorted(probed),
                         ['m-audio', 'm-empty', 'm-legacy', 'm-omni', 'm-text'])
        self.assertTrue(all(c['status'] == 'ok' for c in task['cells'].values()))
        self.assertEqual(task['summary']['skipped'], 0)

    def test_skipped_is_never_a_disable_verdict(self):
        """'skipped' must never enter the disable set — even a future edit to
        _PROBE_DISABLE_STATUSES must not sweep it in."""
        self.assertNotIn('skipped', pp._PROBE_DISABLE_STATUSES)


class BackendNeuterTest(_ProbeRedirect):
    """NEUTER: strip the non-chat guard from a COPY of provider_probe.py →
    the image model IS probed and flagged → proves the guard is load-bearing.
    The shipped module is left byte-identical."""

    def test_neuter_guard_restores_false_positive(self):
        with open(pp.__file__, encoding='utf-8') as f:
            src = f.read()
        anchor = 'if caps and not _is_chat_model(caps):'
        self.assertIn(anchor, src, 'guard anchor drifted — update the neuter')
        neutered = src.replace(anchor, 'if False and caps and not _is_chat_model(caps):', 1)
        self.assertNotEqual(neutered, src)

        tmp_mod = os.path.join(self._tmp.name, 'provider_probe_neutered.py')
        with open(tmp_mod, 'w', encoding='utf-8') as f:
            f.write(neutered)
        spec = importlib.util.spec_from_file_location('provider_probe_neutered', tmp_mod)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.probe_cache_path = lambda pid: os.path.join(self._tmp.name, pid + '.json')

        task = _task()
        work = [(0, 'sk-a', 'img-1', 'img-1', ['image_gen'])]
        task['total'] = 1
        with mock.patch.object(mod, 'probe_one_cell',
                               return_value=('unavailable', 'HTTP 500 bound must be positive')):
            mod.run_cell_probe_task(task, work, timeout=5)

        cell = task['cells'][mod.probe_cell_key(0, 'img-1')]
        self.assertEqual(cell['status'], 'unavailable',
                         'NEUTER did not bite: guard removed but cell still skipped')
        self.assertTrue(cell['recommend_disable'],
                        'NEUTER did not bite: without the guard the false positive '
                        'must again recommend disabling a working image model')

        with open(pp.__file__, encoding='utf-8') as f:
            self.assertEqual(f.read(), src, 'harness mutated the shipped provider_probe.py')


class RouteCapsFlowTest(unittest.TestCase):
    """``probe_provider_cells_start`` must carry model capabilities into the
    work tuples (otherwise the backend skip never sees them)."""

    def test_capabilities_reach_work_tuples(self):
        try:
            import routes.config as cfg
        except ImportError as e:
            pytest.skip(
                'routes.config unimportable under active sibling memory '
                'refactor (pt_229606ca deleted lib/memory/installer.py with a '
                'stale package facade) — auto-activates once the facade lands: %s' % e)
        from quart import Quart

        app = Quart('probe-caps-flow')
        app.register_blueprint(cfg.config_bp)

        captured = {}
        done = threading.Event()

        def stub(task, work, timeout):
            captured['work'] = list(work)
            done.set()

        body = {
            'provider_id': 't-caps-flow',
            'base_url': 'https://gw.example.com/v1',
            'api_keys': ['sk-a', 'sk-b'],
            'models': [
                {'model_id': 'chat-x', 'capabilities': ['text', 'thinking']},
                {'model_id': 'img-1', 'aliases': ['img-1-a'], 'capabilities': ['image_gen']},
            ],
            'attempts': 1,
            'force': True,
        }

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(cfg, '_run_cell_probe_task', side_effect=stub), \
                mock.patch.object(cfg, '_probe_cache_path',
                                  lambda pid: os.path.join(tmp.name, pid + '.json')):
            async def _post():
                async with app.test_client() as client:
                    return await client.post('/api/v1/providers/probe-cells/start', json=body)
            resp = asyncio.run(_post())
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(done.wait(5), 'probe thread stub never ran')

        work = captured['work']
        # 2 keys × (1 root + (root + 1 alias)) = 6 cells.
        self.assertEqual(len(work), 6)
        by_mid = {}
        for item in work:
            self.assertEqual(len(item), 5, 'work item must be a 5-tuple with caps')
            by_mid.setdefault(item[3], item[4])
        self.assertEqual(by_mid['chat-x'], ['text', 'thinking'])
        self.assertEqual(by_mid['img-1'], ['image_gen'])
        self.assertEqual(by_mid['img-1-a'], ['image_gen'],
                         'aliases must inherit the root model capabilities')

        with pp.CELL_PROBE_LOCK:
            pp.CELL_PROBE_TASKS.pop('t-caps-flow', None)


# ── Frontend: run the REAL access_matrix.js under node ──────────────────

def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.document = { querySelector: function () { return null; } };
global.t = function (k) { return k; };
global.escapeHtml = function (s) { return String(s); };
global.showToast = function () {};
global._renderProvidersTab = function () {};
// Declared in settings/local_endpoints.js in production (shared window scope);
// the harness supplies it so we only eval the file under test.
global._stgProviders = [];

eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/model_caps.js
eval(fs.readFileSync(process.argv[3], 'utf8'));  // REAL access_matrix.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Provider with one image model + one chat model.
_stgProviders[0] = {
  id: 'p',
  models: [
    { model_id: 'img-m', capabilities: ['image_gen'] },
    { model_id: 'chat-m', capabilities: ['text'] },
  ],
};

// ── (E) Stale persisted snapshot (pre-fix false positives) heals on ingest ──
const stale = {
  status: 'done',
  cells: {
    '0::img-m':  { key_idx: 0, model_id: 'img-m',  root_model_id: 'img-m',
                   status: 'unavailable',
                   detail: 'HTTP 500 {"data":"bound must be positive"} (3/3 attempts failed)',
                   recommend_disable: true },
    '0::chat-m': { key_idx: 0, model_id: 'chat-m', root_model_id: 'chat-m',
                   status: 'unavailable', detail: 'HTTP 500 x', recommend_disable: true },
  },
  summary: { ok: 0, disable: 2 },
  total: 2, done_count: 2,
};
check('ingest_returns_true', _ingestProbeSnapshot(0, stale) === true);
const probe = _stgMatrixProbe[0];
check('nonchat_downgraded_to_skipped', probe.cells['0::img-m'].status === 'skipped');
check('nonchat_no_longer_recommended', probe.cells['0::img-m'].recommend_disable === false);
check('original_verdict_kept_in_tooltip',
      probe.cells['0::img-m'].detail.indexOf('was unavailable') >= 0 &&
      probe.cells['0::img-m'].detail.indexOf('bound must be positive') >= 0);
check('chat_cell_untouched',
      probe.cells['0::chat-m'].status === 'unavailable' &&
      probe.cells['0::chat-m'].recommend_disable === true);
check('summary_recomputed',
      probe.summary.skipped === 1 && probe.summary.disable === 1 && probe.summary.ok === 0);

// ── (F) Apply guard: even a hand-crafted recommend_disable on a non-chat
//        model (bypassing reconcile entirely) must NOT be applied ──
_stgMatrixProbe[0] = {
  status: 'done',
  cells: {
    '0::img-m':  { key_idx: 0, model_id: 'img-m',  root_model_id: 'img-m',
                   status: 'unavailable', detail: 'x', recommend_disable: true },
    '0::chat-m': { key_idx: 0, model_id: 'chat-m', root_model_id: 'chat-m',
                   status: 'unavailable', detail: 'x', recommend_disable: true },
  },
  summary: { ok: 0, disable: 2 },
};
_applyMatrixRecommendations(0);
const imgModel = _stgProviders[0].models[0];
const chatModel = _stgProviders[0].models[1];
const imgDisabled = imgModel.key_access && imgModel.key_access['0'] &&
  (imgModel.key_access['0'].disabled_ids || []).indexOf('img-m') >= 0;
const chatDisabled = chatModel.key_access && chatModel.key_access['0'] &&
  (chatModel.key_access['0'].disabled_ids || []).indexOf('chat-m') >= 0;
check('apply_never_disables_nonchat', !imgDisabled);
check('apply_still_disables_chat', !!chatDisabled);

console.log(out.join('\n'));
process.exit(0);
"""


def _run_node_harness(matrix_js: str) -> str:
    harness = os.path.join(HERE, '_probe_nonchat_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, MODEL_CAPS_JS, matrix_js],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
class FrontendReconcileTest(unittest.TestCase):

    def test_stale_snapshot_reconciled_and_apply_guarded(self):
        output = _run_node_harness(ACCESS_MATRIX_JS)
        fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
        self.assertEqual(fails, [], 'frontend reconcile/apply-guard failures:\n' + output)
        self.assertGreaterEqual(output.count('PASS'), 8,
                                'expected >=8 PASS lines, got:\n' + output)

    def test_frontend_neuter_reconcile_is_load_bearing(self):
        """NEUTER: drop the _reconcileProbeNonChat call from a COPY of
        access_matrix.js → the stale false positive is NOT healed → proves
        the reconcile call is load-bearing. Shipped file byte-identical."""
        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            src = f.read()
        anchor = '  _reconcileProbeNonChat(provIdx);\n'
        self.assertIn(anchor, src, 'reconcile-call anchor drifted — update the neuter')
        neutered = src.replace(anchor, '', 1)
        self.assertNotEqual(neutered, src)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        copy = os.path.join(tmp.name, 'access_matrix_neutered.js')
        with open(copy, 'w', encoding='utf-8') as f:
            f.write(neutered)

        output = _run_node_harness(copy)
        self.assertIn('FAIL nonchat_downgraded_to_skipped', output,
                      'NEUTER did not bite: stale false positive healed even without '
                      'the reconcile call.\n' + output)

        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            self.assertEqual(f.read(), src, 'harness mutated the shipped access_matrix.js')


if __name__ == '__main__':
    unittest.main()
