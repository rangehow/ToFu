#!/usr/bin/env python3
"""tests/frontend/test_frontend_devices_tab.py — RWA P4b-1:Settings → Devices 页.

钉住的事(每一处都是「页面静默消失」类事故的高发点):
  * 装配链:tab 按钮(index.html data-tab="devices")→ SETTINGS_PANEL:devices
    标记 → static/settings_panels/devices.html(id=settingsTab_devices)→
    settings/devices.js 在 _BUNDLE_FILES → core_panel 的 switchSettingsTab
    钩子 → Api.desktop 域命中三端点 → i18n 键存在;
  * 行为(jsdom):节点列表(权限档/离线/本机进程节点)、移除走 nodes DELETE
    或离线凭据撤销、「添加电脑」打开本机控制配对;不暴露 mint/copy 工作流;
    NEUTER:摘掉 core_panel 钩子 → 切页签不填充。

Run isolated: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/frontend/test_frontend_devices_tab.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests._jsdom import _SHARED_HARNESS, require_node_modules, ROOT, run_harness
from tests._paper_vite import compiled_typescript
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEVICES_TS = Path(PROJECT_ROOT) / 'frontend/src/features/settings/devices.ts'
SETTINGS_TS = Path(PROJECT_ROOT) / 'frontend/src/features/settings.ts'
ESBUILD = Path(PROJECT_ROOT) / 'scripts' / 'vite_test_bundle.mjs'


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


# ══════════════════════════════════════════════════════════════════════
#  1. 装配链静态钉
# ══════════════════════════════════════════════════════════════════════

def test_tab_button_and_panel_marker_in_index():
    html = _read(os.path.join(PROJECT_ROOT, 'index.html'))
    assert 'data-tab="devices"' in html, 'index.html 缺 Devices 页签按钮'
    assert '<!-- SETTINGS_PANEL:devices -->' in html, (
        'index.html 缺 SETTINGS_PANEL:devices 标记 —— 面板不会被注入')


def test_panel_fragment_exists_with_matching_id():
    frag = _read(os.path.join(PROJECT_ROOT, 'static', 'settings_panels',
                              'devices.html'))
    assert 'id="settingsTab_devices"' in frag, (
        '片段 id 必须是 settingsTab_devices(switchSettingsTab 按此约定寻址)')
    for el in ('devicesAgentsList', 'devicesWaitsList'):
        assert el in frag, f'面板缺关键元素 #{el}'
    for retired in ('devicesMintBtn', 'devicesMintedBox',
                    'devicesMintedToken', 'devicesMintName',
                    'devicesTokensList', '受控端'):
        assert retired not in frag, f'手工凭据流程泄回面板: #{retired}'


def test_devices_js_in_bundle_list():
    assert "import './settings/devices';" in SETTINGS_TS.read_text(), (
        '原生 settings/devices.ts 未接入 Vite settings 入口')


_SCOPED_HOOK_BODY = r"""
(async () => {
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' +
    '<button class="settings-tab" data-tab="general"></button>' +
    '<button class="settings-tab" data-tab="devices"></button>' +
    '<div class="settings-tab-panel" id="settingsTab_general"></div>' +
    '<div class="settings-tab-panel" id="settingsTab_devices"></div></body>',
  targets: [process.argv[2]],
  globals: {
    t: (k) => k, escapeHtml: (s) => String(s),
    _fitMatrixPanelWidth: () => {},
    Api: { desktop: { nodes: async () => ({ nodes: [] }) } },
  },
});
let populated = 0;
window._populateDevicesTab = global._populateDevicesTab = () => { populated += 1; };
switchSettingsTab('general');
for (let _i = 0; _i < 6; _i++) await Promise.resolve();
check('other_tab_skips_devices_hook', populated === 0);
check('other_tab_panel_active',
      document.getElementById('settingsTab_general').classList.contains('active'));
switchSettingsTab('devices');
for (let _i = 0; _i < 6; _i++) await Promise.resolve();
check('devices_tab_runs_hook_once', populated === 1);
check('devices_panel_active',
      document.getElementById('settingsTab_devices').classList.contains('active'));
report();
process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_switch_hook_delegates():
    """Only activating the Devices tab runs its populate hook (切到页签不白屏)."""
    owner = native_module_path(
        'settings-core-panel-devices.js',
        ROOT + '/frontend/src/features/settings/core-panel.ts')
    run_harness(target_js=owner, body_js=_SCOPED_HOOK_BODY, expect_pass=4,
                label='devices hook scope')


_DESKTOP_ENDPOINTS_BODY = r"""
const fs = require('fs');
const assert = require('node:assert/strict');
global.window = globalThis;
global.location = { pathname: '/' };
global.document = { getElementById: () => null };
global.sessionStorage = { getItem: () => null, setItem() {} };
(new Function(fs.readFileSync(process.argv[1], 'utf8')))();
const { createEndpointRegistry, apiTransport } = globalThis.desktopRegistryTest;
const api = createEndpointRegistry(apiTransport, () => 'en');
const requests = [];
global.fetch = async (url, options = {}) => {
  requests.push([String(options.method || 'GET').toUpperCase(),
                 new URL(String(url), 'http://localhost').pathname]);
  requests[requests.length - 1].push(new URL(String(url), 'http://localhost').search);
  return new Response(JSON.stringify({ data: { nodes: [] }, meta: {} }),
                      { status: options.method === 'POST' ? 201 : 200,
                        headers: { 'content-type': 'application/json' } });
};
(async () => {
  await api.desktop.nodes();
  await api.desktop.removeNode('node/1');
  await api.desktop.revokeToken('key/1');
  await api.desktop.pairCode('https://ide.example.com/proxy/15000');
  assert.deepEqual(requests, [
    ['GET', '/api/v4/desktop/nodes', ''],
    ['DELETE', '/api/v4/desktop/nodes/node%2F1', ''],
    ['DELETE', '/api/v4/desktop/token/key%2F1', ''],
    ['POST', '/api/v4/desktop/pair-code',
     '?base=' + encodeURIComponent('https://ide.example.com/proxy/15000')],
  ]);
  assert.equal(typeof api.desktop.mintAttachBundle, 'undefined');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


def test_api_domain_owns_node_and_pairing_endpoints():
    """Api.desktop lists nodes, removes one, revokes a credential and mints a
    pairing link with the page's live base (?base=)."""
    entry = """
import { createEndpointRegistry } from './index';
import { apiTransport } from '../transport';
(window as any).desktopRegistryTest = { createEndpointRegistry, apiTransport };
"""
    registry = Path(PROJECT_ROOT) / 'frontend/src/api/endpoint-registry/index.ts'
    with compiled_typescript(registry, contents=entry) as bundle:
        proc = subprocess.run(['node', '-e', _DESKTOP_ENDPOINTS_BODY, bundle],
                              capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_i18n_keys_present_both_langs():
    locale_dir = Path(PROJECT_ROOT) / 'frontend/src/i18n/locales'
    locales = [json.loads((locale_dir / f'{lang}.json').read_text())
               for lang in ('zh', 'en')]
    for key in ('settings.tabDevices', 'devices.colTiers',
                'devices.remove', 'devices.empty', 'local.pairAddBtn',
                'devices.waitsTitle', 'devices.noWaits'):
        assert all(key in locale and locale[key] for locale in locales), \
            f'i18n 缺双语键 {key}'


# ══════════════════════════════════════════════════════════════════════
#  2. 行为(jsdom 真驱)
# ══════════════════════════════════════════════════════════════════════

_DEVICES_BODY = r"""
(async () => {
const { setup } = require(process.env.JSDOM_HARNESS);

const FRAG = process.argv[4];
const fs = require('fs');
const html = '<!DOCTYPE html><body>' + fs.readFileSync(FRAG, 'utf8') + '</body>';

const calls = { get: [], del: [], waits: [], cancel: [], toasts: [] };
const wait = (id, createdAt, name) => ({
  waitId: id, conversationId: 'conv-' + id, reason: 'Run the backup for ' + id,
  createdAt, state: 'pending', device: { selector: name, agentId: null, name: null },
});
// The pending view arrives in pointer (device) order across pages.
const waitPages = {
  '': { waits: [wait('dw-2', 1785000002000, 'winbox')], nextCursor: 'k-1' },
  'k-1': { waits: [wait('dw-1', 1785000001000, 'macbook')], nextCursor: null },
};
let waitsFail = false;
const READ = { read: true, write: false, exec: false, gui: false, egress: false, codex_login: false };
const fixture = {
  nodes: [
    { node_id: 'aaaaaaaabbbb', name: 'macbook', platform: 'darwin', online: true,
      share_roots: [{ name: 'myapp', path: '/code/myapp' }],
      tiers: Object.assign({}, READ, { write: true }),
      credential: { id: 'k_mac', name: 'macbook' }, attachment: 'paired' },
    { node_id: null, name: 'winbox', online: false, share_roots: [],
      tiers: READ, credential: { id: 'k_1', name: 'winbox' }, attachment: 'paired' },
    { node_id: 'proc', name: 'tofu-host', platform: 'linux', online: true,
      share_roots: [], tiers: READ, credential: null, attachment: 'process' },
  ],
};
let opened = 0;

const { check, report } = setup({
  root: process.argv[3],
  html,
  targets: [process.argv[2]],
  globals: {
    t: (k) => k,
    escapeHtml: (s) => String(s),
    showToast: (message) => { calls.toasts.push(message); },
    closeSettings: () => {},
    openLocalControlModal: () => { opened += 1; },
    Api: {
      desktop: {
        nodes: async () => { calls.get.push(1); return fixture; },
        removeNode: async (id) => { calls.del.push('node:' + id); return { removed: id }; },
        revokeToken: async (id) => { calls.del.push('key:' + id); return { revoked: id }; },
        deviceWaits: async (cursor) => {
          calls.waits.push(cursor || '');
          return waitsFail ? null : waitPages[cursor || ''];
        },
        cancelDeviceWait: async (id) => {
          calls.cancel.push(id);
          return { waitId: id, state: 'cancelled' };
        },
      },
    },
  },
});
const owner = globalThis.DevicesModule || window.DevicesModule;
for (const name of ['HTMLElement', 'HTMLButtonElement',
                    'AbortController', 'AbortSignal']) {
  global[name] = window[name];
}
const settle = () => new Promise((resolve) => setImmediate(resolve));

// ── 渲染:三个节点 ──
owner.renderDeviceNodes(fixture.nodes);
const rows = document.querySelectorAll('.devices-agent-row');
check('nodes_three_rows', rows.length === 3);
check('node_offline_marked', rows[1].classList.contains('devices-offline'));
check('node_root_shown', rows[0].innerHTML.includes('myapp'));
check('node_tiers_shown', rows[0].querySelector('.devices-tiers').textContent
      === 'local.tierRead · local.tierWrite'
      && rows[1].querySelector('.devices-tiers').textContent === 'local.tierReadOnly');
check('process_node_has_no_remove', !rows[2].querySelector('button.devices-remove-btn'));
check('no_token_lane', !document.getElementById('devicesTokensList'));
check('no_manual_auth_controls',
      !document.getElementById('devicesMintBtn') &&
      !document.getElementById('devicesMintedToken'));

// ── 添加电脑 → 本机控制配对 ──
document.querySelector('#devicesAgentsList button.devices-add-btn').click();
check('add_machine_opens_local_control', opened === 1);

// ── 移除:live 节点走 nodes DELETE,离线凭据走 token revoke ──
rows[0].querySelector('button.devices-remove-btn').click();
await settle(); await settle();
check('remove_live_node', calls.del[0] === 'node:aaaaaaaabbbb');
document.querySelectorAll('.devices-agent-row')[1]
  .querySelector('button.devices-remove-btn').click();
await settle(); await settle();
check('revoke_offline_credential', calls.del[1] === 'key:k_1');

// ── 空态 ──
owner.renderDeviceNodes([]);
check('empty_state', document.getElementById('devicesAgentsList')
      .innerHTML.includes('devices.empty')
      && !!document.querySelector('#devicesAgentsList button.devices-add-btn'));

// ── device waits: every page, registration order, cancel → POST + refresh ──
// The harness neuters setTimeout; setImmediate runs once queued microtasks drain.
const waitsLane = document.getElementById('devicesWaitsList');
await settle();  // the revoke's refresh settles before this read starts
calls.waits.length = 0;
await owner.populateDevicesTab();
check('waits_follow_every_page', calls.waits.join(',') === ',k-1');
const waitRows = () => [...waitsLane.querySelectorAll('.devices-wait-row')];
check('waits_in_registration_order',
      waitRows().map((row) => row.dataset.waitId).join(',') === 'dw-1,dw-2');
check('wait_row_names_device_reason_and_conversation',
      waitRows()[0].textContent.includes('macbook')
      && waitRows()[0].textContent.includes('Run the backup for dw-1')
      && waitRows()[0].textContent.includes('conv-dw-1'));
const cancelButton = waitRows()[0].querySelector('button.devices-wait-cancel-btn');
check('wait_cancel_button', Boolean(cancelButton)
      && cancelButton.textContent === 'devices.waitCancel');
calls.waits.length = 0;
cancelButton.click();
await settle();
await settle();
check('wait_cancel_posts_then_refreshes', calls.cancel.join(',') === 'dw-1'
      && calls.waits.join(',') === ',k-1'
      && calls.toasts.includes('devices.waitCancelled'));

owner.renderDeviceWaits([]);
check('waits_empty_state', waitsLane.textContent.includes('devices.noWaits'));

waitsFail = true;
await owner.populateDevicesTab();
check('waits_failure_stays_in_its_lane',
      Boolean(waitsLane.querySelector('.devices-load-failed'))
      && document.querySelectorAll('.devices-agent-row').length === 3);

report();
process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _compile_devices(tmp_path):
    require_node_modules('vite')
    built = tmp_path / 'devices.js'
    proc = subprocess.run(
        [str(ESBUILD), str(DEVICES_TS), '--bundle', '--format=iife',
         '--platform=browser', '--global-name=DevicesModule',
         '--footer:js=globalThis.DevicesModule = DevicesModule;',
         f'--outfile={built}'],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return built


def test_devices_tab_behaviour_jsdom(tmp_path):
    built = _compile_devices(tmp_path)
    run_harness(
        target_js=str(built),
        body_js=_DEVICES_BODY,
        extra_targets=[os.path.join(PROJECT_ROOT, 'static',
                                    'settings_panels', 'devices.html')],
        expect_pass=18,
        label='devices tab',
    )


_NEUTER_HOOK_BODY = r"""
(async () => {
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' +
    '<button class="settings-tab" data-tab="devices"></button>' +
    '<div class="settings-tab-panel" id="settingsTab_devices">' +
    '<div id="devicesAgentsList"></div>' +
    '</div></body>',
  targets: [process.argv[2]],
  globals: {
    t: (k) => k, escapeHtml: (s) => String(s),
    _fitMatrixPanelWidth: () => {},
    Api: { desktop: { nodes: async () => ({ nodes: [] }) } },
  },
});
// indirect eval 把被测文件的顶层函数挂到 node global(不挂 window)——
// core_panel 里的裸 typeof 查的是 global,所以桩必须两边都挂。
window._populateDevicesTab = global._populateDevicesTab = () => {
  document.getElementById('devicesAgentsList').innerHTML = 'POPULATED';
};
switchSettingsTab('devices');
for (let _i = 0; _i < 6; _i++) await Promise.resolve();
check('hook_fills_tab',
      document.getElementById('devicesAgentsList').innerHTML === 'POPULATED');
report();
process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_neuter_hook_means_blank_tab():
    """The real tab switch reads the registry hook on each activation."""
    owner = native_module_path(
        'settings-core-panel-devices.js',
        ROOT + '/frontend/src/features/settings/core-panel.ts')
    output = run_harness(
        target_js=owner,
        body_js=_NEUTER_HOOK_BODY,
        min_pass=1,
        label='devices hook present',
    )
    assert 'PASS hook_fills_tab' in output
    output = run_harness(
        target_js=owner,
        body_js=_NEUTER_HOOK_BODY.replace(
            "switchSettingsTab('devices');",
            'delete featureRegistry._populateDevicesTab;\n'
            'delete window._populateDevicesTab; delete global._populateDevicesTab;\n'
            'if (featureRegistry._populateDevicesTab !== undefined) '
            '  throw new Error("Devices registry fallback was not removed");\n'
            "switchSettingsTab('devices');",
        ).replace("check('hook_fills_tab',", "check('neuter_tab_stays_blank',")
        .replace("=== 'POPULATED'", "!== 'POPULATED'"),
        min_pass=1,
        label='devices hook removed',
    )
    assert 'PASS neuter_tab_stays_blank' in output

