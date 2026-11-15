"""User-visible connection state must recover from stale failure signals.

The tests execute the shipped JavaScript under jsdom. They pin one durable UX
contract: the canonical Sidecar warning clears after recovery. The network
badge's state machine is specified in frontend/src/network-indicator.test.ts.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

STORAGE_MONITOR = native_module_path(
    '.native/storage-availability-monitor-contract.js',
    Path(ROOT) / 'frontend/src/storage-availability-monitor.ts',
)
ZH_I18N = os.path.join(ROOT, 'frontend', 'src', 'i18n', 'locales', 'zh.json')


_STORAGE_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.AbortSignal = win.AbortSignal || { timeout: () => undefined };

const out = [];
function check(name, condition) {
  out.push((condition ? 'PASS ' : 'FAIL ') + name);
}

const messages = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
const translate = (key, values) => {
  let result = messages[key] || key;
  for (const [name, value] of Object.entries(values || {})) {
    result = result.replaceAll('{' + name + '}', String(value));
  }
  return result;
};

let storageReady = false;
let recoveryPoll = null;
const schedule = {
  now: () => 0,
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: (callback) => {
    recoveryPoll = callback;
    return 1;
  },
  clearInterval: () => {
    recoveryPoll = null;
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));
if (typeof createStorageAvailabilityMonitor !== 'function') {
  console.log('FAIL typed_storage_monitor_missing');
  process.exit(0);
}
const monitor = createStorageAvailabilityMonitor({
  document,
  schedule,
  log: { debug: () => {}, info: () => {}, warn: () => {}, error: () => {} },
  warningIconHtml: () => '<svg data-warning-icon></svg>',
  isVisible: () => true,
  probeHealth: async () => ({
    ok: true,
    json: async () => ({ storage: { ready: storageReady } }),
  }),
  copy: {
    unavailableTitle: () => translate('conn.storageUnavailableTitle'),
    unavailableDescription: () => translate('conn.storageUnavailableDesc'),
    dismiss: () => translate('conn.dismiss'),
  },
});

(async () => {
  await monitor.check();
  const banner = document.getElementById('storage-warning-banner');
  const html = banner ? banner.innerHTML : '';
  check('banner_shown_when_storage_unready', !!banner);
  check('banner_zh_title', html.includes('存储服务暂时不可用'));
  check('banner_zh_desc', html.includes('正在自动恢复存储连接'));
  check('banner_zh_dismiss', html.includes('>关闭</button>'));
  check('no_database_specific_recovery_advice', !html.includes('PostgreSQL'));

  storageReady = true;
  await monitor.check();
  check('banner_cleared_on_recovery', !document.getElementById('storage-warning-banner'));
  await monitor.check();
  check('healthy_check_is_idempotent', !document.getElementById('storage-warning-banner'));
  monitor.destroy();
  console.log(out.join('\n'));
})();
"""


def _run_harness(tmp_path, name: str, source: str, *args: str) -> str:
    harness = tmp_path / name
    harness.write_text(source, encoding='utf-8')
    result = subprocess.run(
        ['node', str(harness), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout.strip()
    assert result.returncode == 0, f'node failed: {result.stderr}\n{output}'
    failures = [line for line in output.splitlines() if line.startswith('FAIL')]
    assert not failures, output
    return output


def test_storage_banner_clears_on_recovery(tmp_path):
    output = _run_harness(
        tmp_path,
        'storage-health.js',
        _STORAGE_HARNESS,
        STORAGE_MONITOR,
        ROOT,
        ZH_I18N,
    )
    assert output.count('PASS') == 7
