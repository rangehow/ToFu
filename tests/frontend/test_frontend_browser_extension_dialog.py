"""tests/frontend/test_frontend_browser_extension_dialog.py — the browser
extension dialog and the removal of computer control from the UI.

WHAT THIS GUARDS
----------------
1. Computer control ("Local Control" / 本机控制) has no browser entry point
   (owner decision 2026-11-17): no toolbar or mobile row, no dialog, no
   Settings → Devices tab or jump target, no remote-device picker, and its
   deep link (#tofu-agent-relay) is answered with "not available".
2. The browser extension dialog that remains shows ONE instruction chosen by
   detected state, paints a followable floor before any response, keeps it on
   a failed status call, and stops polling when closed.

The renderer runs from the SHIPPED section under jsdom; nothing is copied.
"""
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests._jsdom import require_node_modules
from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent.parent
BE_JS = Path(runtime_section_path('browser-extension.js'))
INDEX_HTML = ROOT / 'index.html'
SETTINGS_PANELS = ROOT / 'static' / 'settings_panels'


def _modal_markup() -> str:
    html = INDEX_HTML.read_text(encoding='utf-8')
    match = re.search(
        r'<div class="modal-overlay" id="browserExtensionModal".*?\n</div>\n', html, re.S)
    assert match, 'index.html lost #browserExtensionModal'
    return match.group(0)


# ── 1. No computer-control entry point remains ───────────────────────────

def test_shell_markup_has_no_computer_control_entry():
    html = INDEX_HTML.read_text(encoding='utf-8')
    for gone in ('localControlToggle', 'mobileLocalControl', 'localControlModal',
                 'openLocalControlModal', 'data-tab="devices"', 'SETTINGS_PANEL:devices',
                 'remoteDevicesSection', 'lcDesktop', 'data-i18n="local.'):
        assert gone not in html, f'index.html still carries {gone!r}'
    assert not (SETTINGS_PANELS / 'devices.html').exists()
    for panel in SETTINGS_PANELS.glob('*.html'):
        text = panel.read_text(encoding='utf-8')
        assert '本机控制' not in text and 'openLocalControlModal' not in text, panel.name


def test_catalogs_carry_no_computer_control_copy():
    for path in (ROOT / 'frontend' / 'src' / 'i18n' / 'locales').glob('*.json'):
        catalog = json.loads(path.read_text(encoding='utf-8'))
        stale = [k for k in catalog if k.startswith(('local.', 'devices.'))
                 or k == 'settings.tabDevices']
        assert not stale, f'{path.name} keeps computer-control keys: {stale[:5]}'
    zh = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text(encoding='utf-8'))
    en = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text(encoding='utf-8'))
    assert not [k for k, v in zh.items() if '本机控制' in v]
    assert not [k for k, v in en.items() if 'Local Control' in v]


def test_devices_is_not_a_jump_target():
    jump = (ROOT / 'frontend/src/features/settings/setting-jump.ts').read_text(encoding='utf-8')
    assert "'devices'" not in jump
    contract = json.loads((ROOT / 'contracts/ui_nav_targets_v1.json').read_text(encoding='utf-8'))
    assert 'devices' not in json.dumps(contract), 'the model-facing directory still offers devices'


def test_computer_control_deep_link_reports_unavailable():
    application = (ROOT / 'frontend/src/application.ts').read_text(encoding='utf-8')
    branch = application[application.index("'#tofu-agent-relay'"):]
    branch = branch[:branch.index('} catch')]
    assert "t('uiNav.jumpUnavailable')" in branch
    assert 'prepareFeature' not in branch, 'the deep link must not load a feature'


# ── 2. The browser extension dialog ──────────────────────────────────────

HARNESS = textwrap.dedent(r"""
(async () => {
  const { JSDOM } = require('jsdom');
  const fs = require('fs');
  const dom = new JSDOM('<!doctype html><body>' + %(html)s + '</body>');
  global.window = dom.window; global.document = dom.window.document;
  global.navigator = dom.window.navigator;
  global.runtimeScope = {};
  global.t = (k) => k;
  global.escapeHtml = (s) => String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  global.BASE_PATH = '/proxy/15000';
  global.apiUrl = (p) => BASE_PATH + p;
  const rec = { clip: null, openExt: 0, opened: [], statusCalls: 0 };
  global._safeClipboardWrite = (s) => { rec.clip = s; return Promise.resolve(); };
  window.open = (url) => { rec.opened.push(url); };
  const MODE = %(mode)s;
  const PAYLOAD = %(payload)s;
  if (MODE !== 'no-api') {
    global.Api = { browser: {
      status: () => { rec.statusCalls++;
        return MODE === 'status-fails' ? Promise.reject(new Error('down'))
          : MODE === 'pending' ? new Promise(() => {}) : Promise.resolve(PAYLOAD); },
      openExtensions: () => { rec.openExt++; return Promise.resolve({ ok: true }); },
    } };
  }
  eval(fs.readFileSync(%(be)s, 'utf8'));

  openBrowserExtensionModal(%(options)s);
  const setup = () => document.getElementById('beBrowserSetup');
  const firstPaint = {
    text: setup().textContent.trim(),
    hasDownload: !!setup().querySelector('#beExtDownloadBtn'),
    status: document.querySelector('#beBrowserStatus .be-status-text').textContent,
  };
  await new Promise((r) => setTimeout(r, 0));
  const download = setup().querySelector('#beExtDownloadBtn');
  if (download) download.onclick();
  const openBtn = setup().querySelector('#beExtOpenBtn');
  if (openBtn) openBtn.onclick();
  await new Promise((r) => setTimeout(r, 0));
  const after = {
    text: setup().textContent.trim(),
    hasDownload: !!setup().querySelector('#beExtDownloadBtn'),
    hasOpen: !!openBtn,
    status: document.querySelector('#beBrowserStatus .be-status-text').textContent,
    dotConnected: !!document.querySelector('#beBrowserStatus .browser-status-dot.connected'),
    note: (document.getElementById('beExtOpenNote') || { textContent: '' }).textContent,
  };
  const open = document.getElementById('browserExtensionModal').classList.contains('open');
  closeBrowserExtensionModal();
  const polling = _bePollTimer !== null;
  console.log(JSON.stringify({ firstPaint, after, rec, open, polling,
    clientId: runtimeScope._browserClientId === undefined ? 'unset' : runtimeScope._browserClientId }));
  process.exit(0);
})();
""")


def _run(payload=None, *, mode='ok', options=None) -> dict:
    node = shutil.which('node')
    require_node_modules('jsdom')
    script = HARNESS % {
        'html': json.dumps(_modal_markup()),
        'be': json.dumps(str(BE_JS)),
        'mode': json.dumps(mode),
        'payload': json.dumps(payload or {}),
        'options': json.dumps(options or {}),
    }
    proc = subprocess.run([node, '-e', script], cwd=ROOT, capture_output=True,
                          text=True, timeout=90)
    assert proc.returncode == 0, f'harness failed:\n{proc.stdout}\n{proc.stderr}'
    return json.loads(proc.stdout.strip().splitlines()[-1])


_LOCAL = {'connected': False, 'extensionPath': '/srv/tofu/browser_extension',
          'localBrowser': {'family': 'chrome', 'name': 'Chrome',
                           'extensionsUrl': 'chrome://extensions'}}


def test_the_first_paint_is_a_followable_instruction():
    out = _run(mode='pending')
    assert out['open']
    assert out['firstPaint']['hasDownload'], out['firstPaint']
    assert out['firstPaint']['status'] == '尚未安装'


def test_the_floor_works_without_an_api_client():
    out = _run(mode='no-api')
    assert out['after']['hasDownload']
    assert len(out['rec']['opened']) == 1
    assert out['rec']['opened'][0].startswith('/proxy/15000/api/browser/download?base=')
    assert out['rec']['opened'][0].endswith('%2Fproxy%2F15000'), 'the ZIP must pair with the live base path'


def test_a_failed_status_call_keeps_the_guidance():
    out = _run(mode='status-fails')
    assert out['after']['hasDownload'], out['after']
    assert out['after']['status'] == '无法连接服务器'


def test_a_connected_current_extension_clears_the_box():
    out = _run({'connected': True, 'secondsAgo': 2, 'servedExtVersion': '5.0.0',
                'clients': [{'client_id': 'c1', 'ext_version': '5.0.0'}]})
    assert out['after']['text'] == ''
    assert out['after']['dotConnected']
    assert out['clientId'] == 'c1'


def test_the_on_disk_case_offers_one_open_action_that_copies_and_opens():
    out = _run(_LOCAL)
    assert out['after']['hasOpen'] and not out['after']['hasDownload']
    assert out['rec']['clip'] == '/srv/tofu/browser_extension'
    assert out['rec']['openExt'] == 1
    assert out['after']['note']


@pytest.mark.parametrize('payload,phrase', [
    ({'connected': True, 'servedExtVersion': '5.1.0',
      'clients': [{'client_id': 'c1', 'ext_version': '5.0.0'}]}, '扩展有新版本'),
    ({'connected': False, 'lockedOutClients': [{'client_id': 'dead'}]}, '凭证失效'),
    ({'connected': False, 'protocolVersion': 2,
      'incompatibleClients': [{'client_id': 'old', 'protocol_version': 1}]}, '协议过旧'),
])
def test_recovery_states_offer_the_download(payload, phrase):
    out = _run(payload)
    assert phrase in out['after']['text'], out['after']['text']
    assert out['after']['hasDownload']


def test_an_adapter_upgrade_request_offers_the_download_while_connected():
    out = _run({'connected': True, 'clients': [{'client_id': 'c1', 'ext_version': ''}]},
               options={'browserUpgrade': True})
    assert '站点适配器所需能力' in out['after']['text']
    assert out['after']['hasDownload']


def test_closing_stops_the_poll():
    out = _run(mode='pending')
    assert out['polling'] is False
