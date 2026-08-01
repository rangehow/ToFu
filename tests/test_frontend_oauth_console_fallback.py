"""tests/test_frontend_oauth_console_fallback.py — the escape hatch is REACHABLE.

The loopback redirect rests on an EXTERNAL fact we cannot verify from this
repo: whether Anthropic accepts ``http://localhost:54545/callback`` for our
client_id. If it ever refuses, a desktop user is hard-blocked — the console
page is what renders ``code#state``, and a loopback flow never reaches it, so
the manual paste box has nothing to paste. ``_oauthCancelAndRetry`` re-runs
the SAME decision, so retrying loops through the identical broken flow.

``TOFU_OAUTH_LOOPBACK=0`` is NOT an answer for the surface that matters most:
the desktop build ships as a packaged executable and its user has nowhere to
set an environment variable.

So this suite pins the property the backend tests are structurally blind to —
that a USER can get out — by driving the SHIPPED js. The previous batch in
this epic shipped a backend fix that the product path could not reach (the
payload the frontend actually sent lacked the fields the backend read); only
an end-to-end guard catches that class.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_OAUTH_JS = _ROOT / 'static' / 'js' / 'settings' / 'oauth.js'
_API_JS = _ROOT / 'static' / 'js' / 'api.js'
_PANEL = _ROOT / 'static' / 'settings_panels' / 'oauth.html'
_I18N = _ROOT / 'static' / 'js' / 'i18n.js'


def _run_node(script: str) -> dict:
    """Execute a node harness and return its JSON verdict."""
    proc = subprocess.run(['node', '-e', script], capture_output=True,
                          text=True, timeout=60, cwd=str(_ROOT))
    if proc.returncode != 0:
        raise AssertionError('node harness failed:\n%s\n%s'
                             % (proc.stdout[-3000:], proc.stderr[-3000:]))
    tail = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith('{')]
    if not tail:
        raise AssertionError('harness printed no verdict:\n%s' % proc.stdout[-3000:])
    return json.loads(tail[-1])


# The harness loads the REAL oauth.js against a minimal DOM, so a rename or a
# dropped argument shows up here rather than in production.
_HARNESS = r'''
const fs = require('fs');
const src = fs.readFileSync(%(oauth_js)s, 'utf8');

const calls = [];
function el(id, extra) {
  const o = Object.assign({ id: id, style: { display: '' }, value: '',
                            onclick: null, textContent: '', disabled: false,
                            classList: { contains: () => false, add(){}, remove(){}, toggle(){} },
                            querySelectorAll: () => [], appendChild(){} }, extra || {});
  return o;
}
const nodes = {};
for (const id of ['oauthClaudeLoginBtn','oauthClaudeLogoutBtn','oauthClaudeStatus',
                  'oauthClaudeInfo','oauthClaudeEmail','oauthClaudeManual',
                  'oauthClaudeAuthUrl','oauthClaudeManualUrl','oauthClaudeEgress',
                  'oauthClaudeCodeHint','oauthClaudePasteRow',
                  'oauthClaudeLoopbackNote','oauthClaudeConsoleFallbackRow',
                  'oauthClaudeConsoleFallbackBtn']) nodes[id] = el(id);

global.document = {
  getElementById: (id) => nodes[id] || null,
  createElement: () => el('tmp'),
  addEventListener: () => {},
};
global.window = { addEventListener: () => {}, open: () => ({ closed: false }) };
global.screen = { width: 1200, height: 900 };
global.setInterval = () => 0;
global.clearInterval = () => {};
global.setTimeout = (f) => { try { f(); } catch (e) {} return 0; };
global.BroadcastChannel = function () { this.onmessage = null; };
global.t = (k) => k;
global.escapeHtml = (s) => String(s);
global.showAlert = () => {};
global.showConfirm = async () => true;
global.debugLog = () => {};
global._loadServerConfig = () => {};
global._safeClipboardWrite = () => Promise.resolve();

const LOGIN_RESPONSE = %(login_response)s;
global.Api = {
  oauth: {
    loginPost: (provider, preferConsole) => {
      calls.push({ verb: 'loginPost', provider, preferConsole: !!preferConsole });
      return Promise.resolve({ ok: true, status: 200,
                               json: () => Promise.resolve(LOGIN_RESPONSE) });
    },
    loginGet: (provider, preferConsole) => {
      calls.push({ verb: 'loginGet', provider, preferConsole: !!preferConsole });
      return Promise.resolve({ ok: true, status: 200,
                               json: () => Promise.resolve(LOGIN_RESPONSE) });
    },
    logoutPost: (provider) => {
      calls.push({ verb: 'logoutPost', provider });
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    },
    status: () => Promise.resolve(null),
    egressAgentGet: () => Promise.resolve({}),
  },
};

eval(src);

(async () => {
  %(body)s
})().catch(e => { console.log(JSON.stringify({ _error: String(e && e.stack || e) })); });
'''


def _harness(body: str, login_response: dict) -> dict:
    return _run_node(_HARNESS % {
        'oauth_js': json.dumps(str(_OAUTH_JS)),
        'login_response': json.dumps(login_response),
        'body': body,
    })


_LOOPBACK_RESP = {'auth_url': 'https://claude.ai/oauth/authorize?x=1',
                  'status': 'started', 'provider': 'claude',
                  'callback_port': 54545, 'redirect_mode': 'loopback',
                  'exchange': {}}
_CONSOLE_RESP = {'auth_url': 'https://claude.ai/oauth/authorize?x=2',
                 'status': 'started', 'provider': 'claude',
                 'callback_port': 54545, 'redirect_mode': 'console',
                 'exchange': {}}


class TestEscapeHatchIsReachable(unittest.TestCase):
    """A user stuck in a loopback flow can reach the console flow."""

    def test_loopback_flow_exposes_the_fallback_control(self):
        v = _harness(
            """
            await _oauthLogin('claude');
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({
              fallbackVisible: document.getElementById('oauthClaudeConsoleFallbackRow').style.display !== 'none',
              handlerWired: typeof document.getElementById('oauthClaudeConsoleFallbackBtn').onclick === 'function',
              noteVisible: document.getElementById('oauthClaudeLoopbackNote').style.display !== 'none',
            }));
            """, _LOOPBACK_RESP)
        self.assertTrue(v['fallbackVisible'],
                        'a loopback flow must offer the way back to manual paste')
        self.assertTrue(v['handlerWired'],
                        'the control must not render as a dead button')
        self.assertTrue(v['noteVisible'])

    def test_clicking_it_restarts_the_flow_with_prefer_console(self):
        """The decisive end-to-end property: the request carries the flag."""
        v = _harness(
            """
            await _oauthLogin('claude');
            await new Promise(r => setImmediate(r));
            document.getElementById('oauthClaudeConsoleFallbackBtn').onclick();
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({ calls: calls }));
            """, _LOOPBACK_RESP)
        logins = [c for c in v['calls'] if c['verb'].startswith('login')]
        self.assertEqual(len(logins), 2, 'clicking must start a FRESH flow')
        self.assertFalse(logins[0]['preferConsole'])
        self.assertTrue(logins[1]['preferConsole'],
                        'the retry must pin the console callback, else it loops '
                        'straight back into the same broken flow')

    def test_fallback_helpers_are_top_level_not_nested(self):
        """They are invoked from the card — a nested decl is unreachable."""
        v = _harness(
            """
            console.log(JSON.stringify({
              apply: typeof _oauthApplyRedirectMode,
              fallback: typeof _oauthUseConsoleFallback,
            }));
            """, _LOOPBACK_RESP)
        self.assertEqual(v['apply'], 'function')
        self.assertEqual(v['fallback'], 'function')


class TestFlowIsDescribedTruthfully(unittest.TestCase):
    """The paste instructions are FALSE during a loopback flow."""

    def test_loopback_hides_the_paste_instructions(self):
        v = _harness(
            """
            await _oauthLogin('claude');
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({
              hint: document.getElementById('oauthClaudeCodeHint').style.display,
              row: document.getElementById('oauthClaudePasteRow').style.display,
            }));
            """, _LOOPBACK_RESP)
        self.assertEqual(v['hint'], 'none',
                         'a loopback flow never renders a code to copy')
        self.assertEqual(v['row'], 'none')

    def test_console_flow_keeps_the_paste_box_and_hides_the_hatch(self):
        """Complement — the fix must not delete the manual flow it falls back to."""
        v = _harness(
            """
            await _oauthLogin('claude');
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({
              hint: document.getElementById('oauthClaudeCodeHint').style.display,
              row: document.getElementById('oauthClaudePasteRow').style.display,
              hatch: document.getElementById('oauthClaudeConsoleFallbackRow').style.display,
              note: document.getElementById('oauthClaudeLoopbackNote').style.display,
            }));
            """, _CONSOLE_RESP)
        self.assertNotEqual(v['hint'], 'none')
        self.assertNotEqual(v['row'], 'none')
        self.assertEqual(v['hatch'], 'none',
                         'offering "switch to manual" while already manual is noise')
        self.assertEqual(v['note'], 'none')

    def test_plain_login_does_not_request_console(self):
        v = _harness(
            """
            await _oauthLogin('claude');
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({ calls: calls }));
            """, _LOOPBACK_RESP)
        logins = [c for c in v['calls'] if c['verb'].startswith('login')]
        self.assertEqual(len(logins), 1)
        self.assertFalse(logins[0]['preferConsole'],
                         'the default must stay the automatic decision')


class TestWiringRatchet(unittest.TestCase):
    """Static pins for the parts the DOM harness cannot observe."""

    def test_api_layer_forwards_the_flag_on_both_transports(self):
        src = _API_JS.read_text(encoding='utf-8')
        m = re.search(r'loginPost:.*?loginGet:.*?\}\),', src, re.S)
        self.assertIsNotNone(m, 'oauth login helpers not found in api.js')
        block = m.group(0)
        self.assertIn('prefer_console: true', block,
                      'POST body must carry the flag')
        self.assertIn("prefer_console: '1'", block,
                      'GET query must carry it too — proxies force the GET '
                      'fallback on exactly the deployments that need it')

    def test_card_markup_has_the_ids_the_renderer_toggles(self):
        html = _PANEL.read_text(encoding='utf-8')
        for node_id in ('oauthClaudeCodeHint', 'oauthClaudePasteRow',
                        'oauthClaudeLoopbackNote',
                        'oauthClaudeConsoleFallbackRow',
                        'oauthClaudeConsoleFallbackBtn'):
            self.assertIn('id="%s"' % node_id, html,
                          'renderer toggles #%s but the card never defines it'
                          % node_id)

    def test_new_strings_are_translated(self):
        i18n = _I18N.read_text(encoding='utf-8')
        for key in ('settings.oauthLoopbackNote', 'settings.oauthUseConsole'):
            self.assertIn("'%s'" % key, i18n, 'missing i18n key %s' % key)


if __name__ == '__main__':
    unittest.main()
