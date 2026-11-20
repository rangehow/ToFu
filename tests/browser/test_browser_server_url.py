"""The extension's single server-address authority (worker/server-url.js).

Users paste their browser address bar into the popup: hash routes, query
strings, ``/index.html``, trailing slashes, no scheme. Ordinary variants
resolve to the Tofu base; reserved route-like prefixes need an explicitly
selected base or a useful refusal, not truncation. Every consumer in the
worker must go through the same authority.
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
    return job.inputs.map(i => vm.runInContext('normalizeTofuServerUrl', ctx)(i, {baseUrl: job.baseUrl}));
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
    job.input, {fetch, timeoutMs: 50, baseUrl: job.baseUrl});
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
    ('ftp://tofu.example/', 'Unsupported scheme'),
    ('javascript:alert(1)', 'is not a web address'),
    ('https://user:pw@tofu.example/', 'user:password@'),
    ('http://exa mple.com', 'is not a valid web address'),
])
def test_rejections_say_what_was_wrong(bad, says):
    [result] = normalize(bad)
    assert result['ok'] is False
    assert says in result['error']


@pytest.mark.parametrize('pasted', [
    'https://demo-user:demo-secret@exa mple.invalid/',
    'demo-user:demo-secret@example.invalid',
    'demo-secret://example.invalid/',
    'https://example.invalid:bad/?token=demo-secret',
])
def test_rejected_address_is_not_echoed_in_diagnostics(pasted):
    out = _node({'kind': 'resolve', 'input': pasted, 'tofuBases': []})
    assert out['resolved']['ok'] is False
    assert out['seen'] == []
    assert 'demo-user' not in out['resolved']['error']
    assert 'demo-secret' not in out['resolved']['error']


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


def test_health_probe_preserves_interior_slashes_in_the_proxy_prefix():
    base = 'https://gateway.example.test/proxy//15000'
    out = _node({'kind': 'resolve', 'input': f' {base}/?view=1#/chat ',
                 'tofuBases': [base]})
    assert out['resolved'] == {'ok': True, 'url': base, 'verified': True,
                               'changed': True}
    assert out['seen'][0] == f'{base}/api/health'


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


@pytest.mark.parametrize('case', ['valid', 'configured'])
def test_startup_polls_configured_address_or_normalized_preseed(case):
    result = run_extension_probe(f'preseedAddress:{case}')
    base = 'https://gateway.example.test/proxy/15000'
    assert result['writes'] and set(result['writes']) == {base}
    assert result['pollHits'] == [f'{base}/api/browser/poll']
    assert result['drivenPollHits'] == [f'{base}/api/browser/poll']
    assert result['healthHits'][0] == f'{base}/api/health'
    assert all('ignored.example.test' not in hit for hit in result['healthHits'])


def test_invalid_preseed_address_is_not_persisted_probed_or_logged_raw():
    result = run_extension_probe('preseedAddress:invalid')
    assert result['writes'] == result['healthHits'] == result['pollHits'] == []
    assert 'fixture-only' not in ' '.join(result['logs'])
    assert 'user:password@' in ' '.join(result['logs'])


def test_overlapping_startup_events_keep_one_client_identity():
    result = run_extension_probe('concurrentIdentityInit')
    assert len(result['writes']) == 2
    assert set(result['writes']) == {result['clientId']}


@pytest.mark.parametrize('prefix', [
    '/api/team/proxy/19091', '/static/team/proxy/19091',
    '/api/team/static/proxy/19091', '/static/team/api/proxy/19091',
    '/proxy//api/team/19091', '/api/health',
])
@pytest.mark.parametrize('suffix', ['', '/', '/#/chat', '/index.html#/chat',
                                    '/api/health', '/static/app.js'])
def test_explicit_selected_base_preserves_intermediary_route_segments(prefix, suffix):
    base = 'http://localhost:19091' + prefix
    result = _node({'kind': 'normalize', 'inputs': [base + suffix], 'baseUrl': base})[0]
    assert result['ok'] is True, result
    assert result['url'] == base
    again = _node({'kind': 'normalize', 'inputs': [result['url']], 'baseUrl': base})[0]
    assert again['url'] == base


@pytest.mark.parametrize('prefix', ['/api/team/proxy/19091', '/static/team/proxy/19091'])
@pytest.mark.parametrize('suffix', ['', '/#/chat', '/api/health', '/static/app.js'])
def test_unknown_reserved_prefix_refuses_without_probing_or_shortening(prefix, suffix):
    out = _node({'kind': 'resolve', 'input': 'http://localhost:19091' + prefix + suffix,
                 'tofuBases': []})
    assert out['seen'] == []
    assert out['resolved']['ok'] is False
    assert 'proxy prefix or an application route' in out['resolved']['error']
    assert 'explicitly configured base' in out['resolved']['error']
    assert 'url' not in out['resolved']


@pytest.mark.parametrize('selected', [
    'http://localhost:19092/api/team',
    'http://localhost:19091/api/tea',
    'http://localhost:19091/api/team-other',
])
def test_selected_base_requires_exact_origin_and_path_boundary(selected):
    out = _node({'kind': 'resolve', 'input': 'http://localhost:19091/api/team/#/chat',
                 'baseUrl': selected, 'tofuBases': []})
    assert out['resolved']['ok'] is False
    assert out['seen'] == []


def test_original_endpoint_pastes_resolve_relative_to_selected_base():
    base = 'http://localhost:19091/proxy/19091'
    for suffix in ['/api/health', '/static/app.js']:
        out = _node({'kind': 'resolve', 'input': base + suffix,
                     'baseUrl': base, 'tofuBases': [base]})
        assert out['resolved']['url'] == base
        assert out['resolved']['verified'] is True


@pytest.mark.parametrize('case', ['prefixConfigured', 'prefixPreseed'])
def test_worker_keeps_explicit_prefix_on_setup_restore_and_popup(case):
    result = run_extension_probe(f'preseedAddress:{case}')
    base = 'http://localhost:19091/api/team/static/proxy/19091'
    assert result['writes'] and set(result['writes']) == {base}
    assert result['pollHits'] == [base + '/api/browser/poll']
    assert result['drivenPollHits'] == [base + '/api/browser/poll']
    assert result['healthHits'][0] == base + '/api/health'
    assert result['popup']['ok'] is True
    assert result['popup']['url'] == base
    assert result['refused']['ok'] is False
    assert result['refusedHealthHits'] == 0
    assert result['afterRefusal'] == base


def test_explicit_base_is_not_replaced_by_a_different_ancestor_app():
    base = 'http://localhost:19091/api/team/static/proxy/19091'
    out = _node({'kind': 'resolve', 'input': base + '/#/chat', 'baseUrl': base,
                 'tofuBases': ['http://localhost:19091']})
    assert out['seen'] == [base + '/api/health']
    assert out['resolved']['ok'] is True
    assert out['resolved']['url'] == base
    assert out['resolved']['verified'] is False
    assert 'saved anyway' in out['resolved']['warning']


@pytest.mark.parametrize('variant', ['proxy', 'bare', 'startupRead', 'startupResolve',
                                    'pendingSave', 'initPreseed', 'initAbsent',
                                    'initPending', 'storedBoolean', 'hydration', 'newEdit'])
def test_popup_save_restore_and_request_keep_base_and_identity(variant):
    result = run_extension_probe(f'popupServerLifecycle:{variant}')
    base = ('https://bridge.invalid' if variant == 'bare'
            else 'https://bridge.invalid/proxy/15000')
    identity = result['initialIdentity']
    assert identity
    saved = result['saved']
    assert saved['stored'] == base
    if variant == 'newEdit':
        assert saved['displayed'] == 'https://second.invalid/proxy/15000/#/chat'
        assert saved['dirty'] == '1'
    else:
        assert saved['displayed'] == base
        assert saved['dirty'] is None
    if variant == 'hydration':
        assert identity == 'synthetic-stable-device'
        assert result['preHydration']['status']['pollActive'] is False
        assert not [req for req in result['preHydration']['requests'] if req['method'] == 'POST']
    assert {'serverUrl': base} in saved['writes']
    late = result['afterLateStartup']
    assert late['stored'] == late['status']['serverUrl'] == base
    assert late['status']['clientId'] == identity
    assert late['requests'] == []
    if variant == 'pendingSave':
        assert result['supersededSave']['ok'] is False
        assert 'superseded' in result['supersededSave']['error']
    assert result['restored']['stored'] == base
    for phase in [saved, result['restored'], result['driven']]:
        assert phase['status']['serverUrl'] == base
        assert phase['status']['clientId'] == identity
        polls = [req for req in phase['requests'] if req['method'] == 'POST']
        assert len(polls) == 1
        assert polls[0]['url'] == base + '/api/browser/poll'
        assert polls[0]['body']['clientId'] == identity
        assert polls[0]['body']['profile'] == 'Synthetic profile'
        assert all('#' not in req['url'] and '/chat' not in req['url']
                   for req in phase['requests'])
    assert result['settingsPreserved'] is True
    refused = result['refused']
    assert refused['requests'] == [] and refused['writes'] == 0
    assert refused['status']['serverUrl'] == result['attached']['serverUrl'] == base
    assert refused['status']['clientId'] == identity
    assert 'not a web address' in refused['note']
    malformed = result['malformed']
    assert malformed['requests'] == malformed['writes'] == []
    assert malformed['status']['serverUrl'] == ''
    assert malformed['status']['clientId'] == identity
    assert malformed['status']['pollActive'] is False


def test_rejected_popup_during_initial_read_preserves_valid_restore():
    result = run_extension_probe('popupServerLifecycle:invalidHydration')
    base = 'https://bridge.invalid/proxy/15000'
    assert 'not a web address' in result['note']
    assert result['stored'] == result['status']['serverUrl'] == base
    assert result['status']['clientId'] == 'synthetic-stable-device'
    assert result['status']['pollActive'] is True
    assert [row['url'] for row in result['requests']] == [
        base + '/api/health', base + '/api/browser/poll']


@pytest.mark.parametrize('variant', ['storageHold', 'storageReject', 'storageSuperseded',
                                     'storageReplacementReject'])
def test_popup_save_requires_persistence_before_adoption(variant):
    result = run_extension_probe(f'popupServerLifecycle:{variant}')
    old = 'https://attached.invalid/proxy/15000'
    base = 'https://bridge.invalid/proxy/15000'
    pending = result['pending']
    assert pending['stored'] == pending['status']['serverUrl'] == old
    assert pending['dirty'] == '1'
    assert pending['button'] != 'Saved'
    assert not [row for row in pending['requests'] if row['method'] == 'POST']
    if variant in ['storageSuperseded', 'storageReplacementReject']:
        committed = result['afterStaleWrite']
        assert committed['stored'] == committed['status']['serverUrl'] == base
        assert [row['url'] for row in committed['requests']] == [base + '/api/browser/poll']
        if variant == 'storageSuperseded':
            assert result['dirty'] is None
            assert result['button'] == 'Saved'
        if variant == 'storageReplacementReject':
            assert result['replacement']['dirty'] == '1'
            assert result['replacement']['button'] != 'Saved'
            assert 'save' in result['replacement']['note'].lower()
            assert result['fresh']['serverUrl'] == base
            assert result['fresh']['requests'] == [base + '/api/health', base + '/api/browser/poll']
            assert result['newerResult']['ok'] is False
            assert result['stored'] == result['status']['serverUrl'] == base
            assert result['requests'] == []
        else:
            assert result['newerResult']['ok'] is True
            assert result['stored'] == result['status']['serverUrl'] == base + '/team'
            assert [row['url'] for row in result['requests']] == [base + '/team/api/browser/poll']
    elif variant == 'storageReject':
        assert result['stored'] == result['status']['serverUrl'] == old
        assert result['dirty'] == '1'
        assert result['displayed'] == base + '/#/chat'
        assert result['button'] != 'Saved'
        assert 'save' in result['note'].lower()
        assert result['requests'] == []
    else:
        assert result['stored'] == result['status']['serverUrl'] == base
        assert result['dirty'] is None
        assert result['button'] == 'Saved'
        assert [row['url'] for row in result['requests']] == [base + '/api/browser/poll']


def test_cold_canonical_restore_needs_no_redundant_write():
    result = run_extension_probe('popupServerLifecycle:coldCanonical')
    base = 'https://bridge.invalid/proxy/15000'
    assert result['status']['serverUrl'] == base
    assert result['status']['clientId'] == 'synthetic-stable-device'
    assert result['status']['pollActive'] is True
    assert result['requests'] == [base + '/api/health', base + '/api/browser/poll']


def test_failed_valid_save_resumes_held_initial_restoration():
    result = run_extension_probe('popupServerLifecycle:failedHydration')
    base = 'https://bridge.invalid/proxy/15000'
    assert 'save' in result['note'].lower()
    assert result['stored'] == result['status']['serverUrl'] == base
    assert result['status']['clientId'] == 'synthetic-stable-device'
    assert result['status']['pollActive'] is True
    assert [row['url'] for row in result['requests'] if row['method'] == 'POST'] == [base + '/api/browser/poll']


def test_storage_read_failure_responds_and_later_popup_save_recovers():
    result = run_extension_probe('popupServerLifecycle:readRecovery')
    first, second = result['first'], result['second']
    old = 'https://attached.invalid/proxy/15000'
    base = 'https://bridge.invalid/proxy/15000'
    assert first['stored'] == first['status']['serverUrl'] == old
    assert first['dirty'] == '1'
    assert first['disabled'] is False
    assert 'read' in first['note'].lower()
    assert 'synthetic read rejection' not in first['note']
    assert first['writes'] == []
    assert not [row for row in first['requests'] if row['method'] == 'POST']
    assert result['reads'] == 2
    assert second['stored'] == second['status']['serverUrl'] == base + '/team'
    assert second['dirty'] is None
    assert second['disabled'] is False
    assert second['button'] == 'Saved'
    assert [row['url'] for row in second['requests'] if row['method'] == 'POST'] == [
        base + '/team/api/browser/poll']
def test_rejected_save_read_resumes_held_initial_restoration():
    result = run_extension_probe('popupServerLifecycle:readFailedHydration')
    base = 'https://bridge.invalid/proxy/15000'
    assert 'read' in result['note'].lower()
    assert result['status']['serverUrl'] == base
    assert result['status']['clientId'] == 'synthetic-stable-device'
    assert result['status']['pollActive'] is True
    assert [row['url'] for row in result['requests'] if row['method'] == 'POST'] == [
        base + '/api/browser/poll']