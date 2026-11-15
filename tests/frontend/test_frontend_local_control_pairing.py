"""Local Control's node list and "Add a machine" pairing flow.

Every Tofu desktop install is a node that joins a server by redeeming a
one-time pairing link (contracts/desktop_node_v1.schema.json). The panel
lists the caller's nodes — several are normal — and mints the link on one
explicit click. These pin the user-visible contract:

* the mint carries ``?base=`` = the page's live origin + BASE_PATH, so the
  link works under /proxy/<port>/ cloud-IDE gateways;
* ``setup_state: 'pair'`` shows ONE instruction, the visitor-OS installer
  row and the button; after the click the link is shown with Copy, the
  6-digit code and a countdown, and a node that appears retires the link;
* every node renders with OS, online dot and granted tiers (read-only by
  default); Remove calls the nodes DELETE route, or revokes the credential
  of an offline paired node with no node id; the in-process node of the
  packaged app has no Remove (the server answers 409);
* nothing of the retired controlled-end installer flow survives.

Loads the REAL shipped local-control.js (plus the retained badge section)
under jsdom.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tests._jsdom import require_node_modules
from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
LOCAL_CONTROL = runtime_section_path('local-control.js')
LOCAL_CONTROL_STATE = runtime_section_path('local-control-state.js')


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const MODE = process.argv[4] || 'normal';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>'
  + '<div id="localControlModal">'
  + '<div id="lcBrowserStatus"><span class="browser-status-dot"></span><span class="lc-status-text"></span></div>'
  + '<div id="lcBrowserAbout"></div><div id="lcBrowserSetup"></div>'
  + '<div id="lcDesktopStatus"><span class="browser-status-dot"></span><span class="lc-status-text"></span></div>'
  + '<button id="lcDesktopSwitch"></button><div id="lcDesktopAbout"></div>'
  + '<div id="lcDesktopSetup"></div><p id="lcPermNote" style="display:none"></p>'
  + '</div><div id="localControlBadge"></div><div id="localControlToggle"></div>'
  + '</body>', { url: 'https://ide.example.com/proxy/15000/' });
global.window = dom.window; global.document = dom.window.document;
global.navigator = dom.window.navigator;
global.runtimeScope = global;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.t = (k) => k;   // fall back to the literal (zh) fallback strings
global.BASE_PATH = '/proxy/15000';
global.apiUrl = (p) => '/proxy/15000' + p;
global.desktopEnabled = false;
global.LocalControlShellState = { get desktopEnabled() { return !!global.desktopEnabled; } };
let clock = 1800000000000;
Date.now = () => clock;
global.setInterval = () => 1;     // the poll + countdown are driven by hand
global.clearInterval = () => {};
dom.window.confirm = () => true;
const calls = { pair: [], remove: [], revoke: [], clip: [], toast: [] };
global._safeClipboardWrite = (s) => { calls.clip.push(s); return Promise.resolve(); };
global.showToast = (m) => { calls.toast.push(m); };
global.Api = {
  browser: { status: () => new Promise(() => {}) },
  desktop: {
    status: () => new Promise(() => {}),
    pairCode: (base) => { calls.pair.push(base); return Promise.resolve({
      code: '123456', ttl: 300, expires_at: 0, pending: [],
      link: 'https://ide.example.com/proxy/15000/#tofu-pair=SECRETabc123' }); },
    removeNode: (id) => { calls.remove.push(id); return Promise.resolve({ removed: id }); },
    revokeToken: (id) => { calls.revoke.push(id); return Promise.resolve({ revoked: id }); },
    listDiags: () => Promise.resolve({ entries: [] }),
    submitDiag: () => Promise.resolve({}),
  },
};

let src = fs.readFileSync(process.argv[1], 'utf8') + '\n' + fs.readFileSync(process.argv[2], 'utf8');
function neuter(from, to) {
  const before = src; src = src.replace(from, to);
  if (src === before) { console.log('NEUTER_NOMUT ' + from); process.exit(2); }
}
if (MODE === 'neuter-base') neuter('Api.desktop.pairCode(_lcPageBase())', "Api.desktop.pairCode('')");
if (MODE === 'neuter-remove') neuter('var call = nodeId ? Api.desktop.removeNode(nodeId) : Api.desktop.revokeToken(keyId);',
                                     'var call = Api.desktop.revokeToken(keyId || nodeId);');
eval(src);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const settle = () => new Promise((r) => setImmediate(r));
const setup = () => document.getElementById('lcDesktopSetup');

const FULL = { os: 'macos', arch: 'arm64', label: 'macOS (Apple silicon)',
  filename: 'Tofu-0.17.0-macos-arm64.dmg',
  url: 'https://h/api/v4/desktop/download/Tofu-0.17.0-macos-arm64.dmg',
  hosted: 'server', size: 152000000 };
const READ = { read: true, write: false, exec: false, gui: false, egress: false, codex_login: false };
const PAIR = { connected: false, setup_state: 'pair', visitor_os: 'macos',
  download_url: 'https://github.com/x/y/releases/latest',
  downloads: [FULL], nodes: [], server_url_reachability: 'private',
  server_bind: 'all' };

(async () => {
// ── 1. first frame: the Add-a-machine button needs no backend ──
openLocalControlModal();
check('floor_has_mint_button', !!document.getElementById('lcPairMintBtn'));
check('floor_one_instruction', setup().querySelectorAll('.lc-step').length === 1);
check('floor_teaches_join_a_server', setup().textContent.includes('加入服务器'));

// ── 2. setup_state pair: one instruction + installer row + button ──
_lcRenderDesktop(PAIR, null);
const html2 = setup().innerHTML;
check('pair_one_instruction', setup().querySelectorAll('.lc-step').length === 1);
check('pair_installer_row_rebased',
  !!setup().querySelector('a.lc-dl-direct[href="/proxy/15000/api/v4/desktop/download/Tofu-0.17.0-macos-arm64.dmg"]'));
check('pair_primary_mint_button',
  !!setup().querySelector('#lcPairMintBtn.btn-primary'));
check('pair_no_link_before_click', !document.getElementById('lcPairLink'));
check('pair_no_controlled_end', !/受控端|TofuRemote|agent-installer|lcAgentInstallerBtn/.test(html2));
check('pair_status_not_connected',
  !document.querySelector('#lcDesktopStatus .browser-status-dot.connected'));
check('pair_no_bind_warning_on_open_bind', !html2.includes('BIND_HOST'));
check('pair_switch_inert', document.getElementById('lcDesktopSwitch').disabled === true);

// ── 3. click: mint with ?base=, then the link + code + countdown ──
document.getElementById('lcPairMintBtn').click();
await settle(); await settle();
check('mint_called_once', calls.pair.length === 1);
check('mint_base_is_live_origin_plus_prefix', calls.pair[0] === 'https://ide.example.com/proxy/15000');
const link = document.getElementById('lcPairLink');
check('link_rendered', !!link && link.textContent === 'https://ide.example.com/proxy/15000/#tofu-pair=SECRETabc123');
check('code_secondary', (document.getElementById('lcPairCode') || {}).textContent === '123 456');
check('countdown_five_minutes', (document.getElementById('lcPairCountdown') || {}).textContent.includes('5:00'));
check('mint_button_replaced_by_link', !document.getElementById('lcPairMintBtn'));
document.getElementById('lcPairCopy').click();
await settle();
check('copy_writes_the_link', calls.clip[0] === 'https://ide.example.com/proxy/15000/#tofu-pair=SECRETabc123');
// An unchanged poll beat keeps the link on screen.
_lcRenderDesktop(PAIR, null);
check('poll_keeps_link', !!document.getElementById('lcPairLink'));

// ── 4. the node appears: link retired, node listed, machine named ──
const JOINED = Object.assign({}, PAIR, { connected: true, setup_state: 'connected',
  nodes: [{ node_id: 'n-mac', name: 'macbook', online: true, platform: 'darwin',
            tiers: READ, credential: { id: 'k-mac', name: 'macbook' }, attachment: 'paired' }] });
_lcRenderDesktop(JOINED, null);
check('joined_link_retired', !document.getElementById('lcPairLink'));
check('joined_named', setup().textContent.includes('「macbook」已加入'));
check('joined_node_listed', setup().querySelectorAll('li.lc-node').length === 1);
check('joined_status_connected',
  !!document.querySelector('#lcDesktopStatus .browser-status-dot.connected'));
check('connected_no_instruction', setup().querySelectorAll('.lc-step').length === 0);
check('connected_add_another_secondary',
  !!setup().querySelector('#lcPairMintBtn.btn-secondary'));
check('perm_note_shown_with_nodes', document.getElementById('lcPermNote').style.display !== 'none');

// ── 5. multiple nodes: process + paired + offline credential ──
const MULTI = Object.assign({}, JOINED, { nodes: [
  { node_id: 'n-proc', name: 'tofu-server', online: true, platform: 'linux',
    tiers: READ, credential: null, attachment: 'process' },
  { node_id: 'n-win', name: 'office-pc', online: true, platform: 'win32',
    tiers: Object.assign({}, READ, { write: true, exec: true }),
    credential: { id: 'k-win', name: 'office-pc' }, attachment: 'paired' },
  { node_id: null, name: 'old-laptop', online: false, last_seen: null,
    tiers: READ, credential: { id: 'k-old', name: 'old-laptop' }, attachment: 'paired' },
] });
_lcRenderDesktop(MULTI, null);
const rows = [...setup().querySelectorAll('li.lc-node')];
check('multi_three_rows', rows.length === 3);
check('multi_status_counts_online',
  document.querySelector('#lcDesktopStatus .lc-status-text').textContent === '已连接 2 个');
check('process_node_named_this_machine', rows[0].textContent.includes('本机'));
check('process_node_has_no_remove', !rows[0].querySelector('button.lc-node-remove'));
check('os_labels', rows[0].textContent.includes('Linux') && rows[1].textContent.includes('Windows'));
check('read_only_by_default', rows[0].querySelector('.lc-node-tiers').textContent === '只读');
check('granted_tiers_listed',
  rows[1].querySelector('.lc-node-tiers').textContent === '读文件 · 写文件 · 运行命令');
check('online_dot', !!rows[1].querySelector('.browser-status-dot.connected')
  && !!rows[2].querySelector('.browser-status-dot.disconnected'));
check('offline_marked', rows[2].textContent.includes('离线'));

// ── 6. Remove: nodes DELETE for a live node, token revoke for an offline credential ──
rows[1].querySelector('button.lc-node-remove').click();
await settle();
check('remove_calls_nodes_delete', calls.remove.join(',') === 'n-win' && calls.revoke.length === 0);
_lcDesktopSigLast = null;
_lcRenderDesktop(MULTI, null);
[...setup().querySelectorAll('li.lc-node')][2].querySelector('button.lc-node-remove').click();
await settle();
check('offline_credential_revoked', calls.revoke.join(',') === 'k-old');

// ── 7. loopback bind warns while pairing; expiry asks for a new link ──
_lcRenderDesktop(Object.assign({}, PAIR, { server_bind: 'loopback' }), null);
check('loopback_bind_warns', setup().innerHTML.includes('BIND_HOST=127.0.0.1'));
_lcRenderDesktop(Object.assign({}, PAIR, { server_url_reachability: 'public' }), null);
check('sso_relay_hint_kept', !!document.getElementById('lcAgentRelayStart'));
document.getElementById('lcPairMintBtn').click();
await settle(); await settle();
clock += 301 * 1000;
_lcRepaintDesktop();
check('expired_link_hidden', !document.getElementById('lcPairLink'));
check('expired_offers_new_mint', !!document.getElementById('lcPairMintBtn'));

console.log(out.join('\n'));
process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run(mode: str = 'normal') -> str:
    require_node_modules('jsdom')
    proc = subprocess.run(
        ['node', '-e', _HARNESS, LOCAL_CONTROL, LOCAL_CONTROL_STATE,
         ROOT, mode],
        capture_output=True, text=True, timeout=60, cwd=ROOT,
    )
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


def test_pairing_and_node_list_matrix():
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'pairing matrix failures:\n' + output
    assert output.count('PASS') == 41, output


def test_NEUTER_dropping_the_base_query_is_caught():
    output = _run('neuter-base')
    assert 'FAIL mint_base_is_live_origin_plus_prefix' in output, output


def test_NEUTER_revoking_instead_of_removing_the_node_is_caught():
    output = _run('neuter-remove')
    assert 'FAIL remove_calls_nodes_delete' in output, output


def test_the_retired_controlled_end_flow_is_gone():
    src = open(LOCAL_CONTROL, encoding='utf-8').read()
    for retired in ('agent-installer', 'agent_downloads', 'agent_installer_ready',
                    'mintAttachBundle', '/v1/attach', "'tray'", "'local_source'",
                    "'remote'", '受控端'):
        assert retired not in src, f'retired controlled-end UI survives: {retired}'
