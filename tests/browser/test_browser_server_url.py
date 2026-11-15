"""The extension's single server-address authority (worker/server-url.js).

Users paste their browser address bar into the popup: hash routes, query
strings, ``/index.html``, trailing slashes, no scheme. Every variant must
resolve to the real Tofu base with any reverse-proxy prefix preserved, and
every consumer in the worker must go through the same authority.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._browser_extension_probe import run_extension_probe

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'browser_extension/worker/server-url.js'
OWNER = ('https://67dc97fd-5f05-4089-8463-a7582066c62e-vscode-shxstraining'
         '.mlp.internal.example.com/proxy/15000')

# The module runs in a bare VM realm like importScripts loads it: no Node
# globals beyond what a service worker also has.
_DRIVER = r"""
const fs = require('node:fs'), vm = require('node:vm');
const ctx = vm.createContext({URL, AbortController, setTimeout, clearTimeout});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx, {filename: process.argv[1]});
const job = JSON.parse(process.argv[2]);
(async () => {
  if (job.kind === 'normalize') {
    return job.inputs.map(i => vm.runInContext('normalizeTofuServerUrl', ctx)(i));
  }
  const seen = [];
  const fetch = async (url, opts) => {
    seen.push(url);
    const base = url.slice(0, -'/api/health'.length);
    if (job.tofuBases.includes(base)) {
      return {ok: true, status: 200, json: async () => ({ok: true, version: '1'})};
    }
    if (job.hang) {
      return new Promise((_, reject) => opts.signal.addEventListener(
        'abort', () => reject(new Error('aborted'))));
    }
    return {ok: true, status: 200, json: async () => { throw new SyntaxError('html'); }};
  };
  const resolved = await vm.runInContext('resolveTofuServerUrl', ctx)(
    job.input, {fetch, timeoutMs: 50});
  return {resolved, seen};
})().then(r => process.stdout.write(JSON.stringify(r)),
          e => { console.error(e); process.exit(1); });
"""


def _node(job):
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for the extension worker realm')
    out = subprocess.run([node, '-e', _DRIVER, str(MODULE), json.dumps(job)],
                         cwd=ROOT, check=True, capture_output=True, text=True,
                         timeout=15)
    return json.loads(out.stdout)


def normalize(*inputs):
    return _node({'kind': 'normalize', 'inputs': list(inputs)})


@pytest.mark.parametrize('pasted, expected', [
    (OWNER, OWNER),
    (OWNER + '/', OWNER),
    (OWNER + '/#/chat', OWNER),
    (OWNER + '#/chat', OWNER),
    (OWNER + '/?x=1', OWNER),
    (OWNER + '/?x=1#/chat', OWNER),
    (OWNER + '/index.html', OWNER),
    (OWNER + '/index.html#/chat', OWNER),
    (OWNER + '/api/health', OWNER),
    (OWNER + '/static/app.js', OWNER),
    ('  ' + OWNER + '/#/chat\n', OWNER),
    (OWNER[len('https://'):] + '/#/chat', OWNER),
    ('localhost:15080', 'http://localhost:15080'),
    ('localhost:15080/#/chat', 'http://localhost:15080'),
    ('http://localhost:15080/', 'http://localhost:15080'),
    ('10.1.2.3:15000', 'http://10.1.2.3:15000'),
    ('http://10.1.2.3:15000/index.html?x=1', 'http://10.1.2.3:15000'),
    ('[::1]:15000', 'http://[::1]:15000'),
    ('127.0.0.1', 'http://127.0.0.1'),
    ('tofu.example.com', 'https://tofu.example.com'),
    ('HTTPS://Tofu.Example.com:443/proxy/15000/', 'https://tofu.example.com/proxy/15000'),
])
def test_address_bar_variants_normalize_to_the_base(pasted, expected):
    [result] = normalize(pasted)
    assert result['ok'] is True, result
    assert result['url'] == expected


def test_normalization_is_idempotent():
    first = normalize(OWNER + '/index.html?x=1#/chat')[0]['url']
    assert normalize(first)[0]['url'] == first


@pytest.mark.parametrize('bad, says', [
    ('', 'Enter your Tofu server address'),
    ('   ', 'Enter your Tofu server address'),
    ('ftp://tofu.example/', 'Unsupported scheme "ftp://"'),
    ('javascript:alert(1)', '"javascript:" is not a web address'),
    ('https://user:pw@tofu.example/', 'user:password@'),
    ('http://exa mple.com', 'is not a valid web address'),
])
def test_rejections_say_what_was_wrong(bad, says):
    [result] = normalize(bad)
    assert result['ok'] is False
    assert says in result['error']


def test_probe_finds_the_real_base_under_an_unknown_suffix():
    out = _node({'kind': 'resolve', 'input': OWNER + '/some/page#/chat',
                 'tofuBases': [OWNER]})
    assert out['resolved'] == {'ok': True, 'url': OWNER, 'verified': True,
                               'changed': True}
    origin = OWNER[:-len('/proxy/15000')]
    assert [u[:-len('/api/health')] for u in out['seen']] == [
        OWNER + '/some/page', OWNER + '/some', OWNER, origin + '/proxy', origin]


def test_probe_prefers_the_most_specific_tofu_base():
    origin = OWNER[:-len('/proxy/15000')]
    out = _node({'kind': 'resolve', 'input': OWNER + '/#/chat',
                 'tofuBases': [origin, OWNER]})
    assert out['resolved']['url'] == OWNER


def test_missing_scheme_probes_the_other_scheme_too():
    out = _node({'kind': 'resolve', 'input': 'tofu.example:15000',
                 'tofuBases': ['https://tofu.example:15000']})
    assert out['resolved']['url'] == 'https://tofu.example:15000'
    assert out['resolved']['verified'] is True


def test_unreachable_server_keeps_lexical_base_and_says_why():
    out = _node({'kind': 'resolve', 'input': OWNER + '/#/chat',
                 'tofuBases': [], 'hang': True})
    resolved = out['resolved']
    assert resolved['ok'] is True and resolved['verified'] is False
    assert resolved['url'] == OWNER
    assert 'timed out' in resolved['warning'] and OWNER in resolved['warning']


def test_every_worker_entry_point_uses_the_resolver():
    """Popup save, a stored legacy raw value and an open Tofu tab all reach
    the same base; a bad scheme is refused without dropping the server."""
    result = run_extension_probe('serverAddress')
    assert result['popup'] == {'ok': True, 'url': OWNER, 'verified': True,
                               'changed': True}
    assert result['afterPopup'] == {'server': OWNER, 'stored': OWNER}
    assert result['rejected']['ok'] is False
    assert 'Unsupported scheme' in result['rejected']['error']
    assert result['afterReject'] == OWNER
    assert result['restored'] == OWNER
    assert result['detected'] == OWNER
