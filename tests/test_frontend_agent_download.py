"""tests/test_frontend_agent_download.py — Local Control's two-component
download matrix (A3, docs/DESKTOP_AGENT_DIST_DESIGN.md §4.7).

The remote branch of ``_lcRenderDesktop`` must offer the AGENT installer
as the PRIMARY action (受控端·轻量 — the machine's role in that branch is
to be controlled) inside a NUMBERED three-step flow, with the full desktop
app collapsed into a one-line <details> secondary — and must degrade to
the historical full-installer rendering when no agent artifact exists yet
(stale-while-build, never a dead end). Pinned:

  1. agent artifact present ⇒ numbered steps (①②③), agent link first
     (受控端·轻量 + 服务器直连 + size), mint button labelled 生成连接行,
     full link inside a COLLAPSED details, escape hatch once;
  1b. preseeded artifact + open bridge ⇒ ZERO-TOUCH: no mint button, the
     auto-connect copy instead; a required bridge token forces the
     3-step flow back even with a preseed;
  2. no agent_downloads ⇒ historical full-installer rendering, no agent
     vocabulary (never an empty primary slot);
  3. local_source ⇒ full primary, no agent mention;
  4. NEUTER (the agent branch severed) ⇒ the primary-agent checks fail —
     the suite discriminates.

Loads the REAL shipped local-control.js under jsdom; skips when
node+jsdom are absent (same convention as test_frontend_cmd_collapse.py).
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const NEUTER = process.argv[4] === 'neuter';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>'
  + '<div id="lcDesktopStatus"><span class="browser-status-dot"></span>'
  + '<span class="lc-status-text"></span></div>'
  + '<div id="lcDesktopSwitch"></div><div id="lcDesktopAbout"></div>'
  + '<div id="lcPermNote"></div><div id="lcDesktopSetup"></div>'
  + '<div id="localControlBadge"></div><div id="localControlToggle"></div>'
  + '</body>', { url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.t = (k) => k;   // fall back to the literal fallback strings
global.browserEnabled = false; global.desktopEnabled = false;

let src = fs.readFileSync(process.argv[2], 'utf8');
if (NEUTER) {
  const before = src;
  src = src.replace('if (agentPicks.length) {', 'if (false) {');
  if (src === before) { console.log('NEUTER_NOMUT'); process.exit(2); }
}
eval(src);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const FULL = { os: 'windows', arch: 'x86_64', label: 'Windows installer',
  filename: 'Tofu-Setup-0.16.0-win64.exe',
  url: 'https://h/api/v1/desktop/download/Tofu-Setup-0.16.0-win64.exe',
  hosted: 'server', size: 152000000, source: 'built', kind: 'full' };
const AGENT = { os: 'windows', arch: 'x86_64',
  label: 'Windows agent installer',
  filename: 'TofuAgent-Setup-0.16.0-win64.exe',
  url: 'https://h/api/v1/desktop/download/TofuAgent-Setup-0.16.0-win64.exe',
  hosted: 'server', size: 53000000, source: 'built', kind: 'agent' };

// ── 1. remote + agent artifact ⇒ agent PRIMARY in a numbered flow ──
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://tofu.example.com/',
  downloads: [FULL], agent_downloads: [AGENT] }, null);
const html1 = document.getElementById('lcDesktopSetup').innerHTML;
check('remote_agent_link_present', html1.includes('TofuAgent-Setup-0.16.0-win64.exe'));
check('remote_agent_label', html1.includes('受控端·轻量'));
check('remote_step1_numbered', html1.includes('① 下载并安装受控端'));
check('remote_steps_numbered', html1.includes('②') && html1.includes('③'));
check('remote_full_secondary_toggle', html1.includes('下载完整桌面版'));
check('remote_full_collapsed',
  html1.includes('<details class="lc-details"><summary>') &&
  !html1.includes('<details class="lc-details" open'));
check('remote_full_link_present', html1.includes('Tofu-Setup-0.16.0-win64.exe'));
check('remote_agent_before_full',
  html1.indexOf('TofuAgent-Setup') < html1.indexOf('Tofu-Setup'));
check('remote_mint_button', html1.includes('lcMintBtn'));
check('remote_mint_label', html1.includes('生成连接行'));
check('remote_hosted_chip_twice',
  (html1.match(/服务器直连/g) || []).length === 2);
check('remote_escape_hatch_once',
  (html1.match(/查看全部下载/g) || []).length === 1);
check('remote_agent_size_shown', html1.includes('50.5 MB') || html1.includes('50.4 MB'));

// ── 1b. preseeded artifact + open bridge ⇒ ZERO-TOUCH (2 steps) ──
const AGENT_PRE = Object.assign({}, AGENT,
  { preseed_url: 'https://tofu.example.com' });
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://tofu.example.com/',
  bridge_token_required: false,
  downloads: [FULL], agent_downloads: [AGENT_PRE] }, null);
const html1b = document.getElementById('lcDesktopSetup').innerHTML;
check('autoconnect_hides_mint', !html1b.includes('lcMintBtn'));
check('autoconnect_copy', html1b.includes('会自动连上'));
// …but a REQUIRED bridge token forces the mint-and-paste flow back even
// with a usable preseed (the agent still needs a credential).
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://tofu.example.com/',
  bridge_token_required: true,
  downloads: [FULL], agent_downloads: [AGENT_PRE] }, null);
const html1c = document.getElementById('lcDesktopSetup').innerHTML;
check('token_required_keeps_mint', html1c.includes('lcMintBtn'));
check('token_required_no_autoconnect', !html1c.includes('会自动连上'));

// ── 2. remote WITHOUT agent artifact ⇒ historical fallback, no dead end ──
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://tofu.example.com/',
  downloads: [FULL], agent_downloads: [] }, null);
const html2 = document.getElementById('lcDesktopSetup').innerHTML;
check('fallback_full_link_primary', html2.includes('Tofu-Setup-0.16.0-win64.exe'));
check('fallback_no_agent_step', !html2.includes('① 下载并安装受控端'));
check('fallback_historical_text', html2.includes('安装桌面版'));
check('fallback_mint_kept', html2.includes('lcMintBtn'));

// ── 3. local_source ⇒ full primary, zero agent vocabulary ──
_lcRenderDesktop({ connected: false, setup_state: 'local_source',
  download_url: 'https://github.com/x/y/releases/latest',
  downloads: [FULL], agent_downloads: [AGENT] }, null);
const html3 = document.getElementById('lcDesktopSetup').innerHTML;
check('local_source_full_link', html3.includes('Tofu-Setup-0.16.0-win64.exe'));
check('local_source_no_agent_link', !html3.includes('TofuAgent-Setup'));
check('local_source_no_agent_gloss', !html3.includes('受控端'));

console.log(out.join('\n'));
process.exit(0);
"""


def _run_harness(neuter: bool) -> str:
    harness = os.path.join(HERE, '_agent_download_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'local-control.js'),       # argv[2]
             ROOT,                                           # argv[3]
             'neuter' if neuter else 'normal'],              # argv[4]
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_agent_download_matrix():
    output = _run_harness(neuter=False)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'agent download matrix failures:\n' + output
    assert output.count('PASS') >= 20, f'expected >=20 PASS lines, got:\n' \
                                       f'{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_severing_the_agent_branch_is_caught():
    output = _run_harness(neuter=True)
    fails = [ln for ln in output.splitlines()
             if ln.startswith('FAIL remote_agent')]
    assert len(fails) >= 3, (
        'the agent-branch neuter should fail the primary-agent checks — '
        'the suite cannot tell the matrix from the fallback:\n' + output)
