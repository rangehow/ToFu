#!/usr/bin/env python3
"""Access-matrix cell toggle resolves its credential in provider scope.

The matrix numbers credential columns by their position inside ONE provider
access (``context.credentials`` is the global array filtered by
``provider_access_id``). ``_toggleMatrixAccess`` received that per-access
column index but indexed the UNFILTERED global ``_stgModelRouting.credentials``
array, so for any provider whose credentials do not start at global index 0
the click edited another access's credential and the clicked dot never
flipped. ``_applyMatrixRecommendations`` already resolves the same index
through ``context.credentials[i].row``; the toggle must match.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests._runtime_sections import ROOT, runtime_section

pytestmark = pytest.mark.unit

NODE = shutil.which('node')


def _run_node(harness_path, *args):
    result = subprocess.run(
        [NODE, str(harness_path), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.skipif(not NODE, reason='node not installed')
def test_toggle_targets_provider_scoped_credential(tmp_path):
    provider = tmp_path / 'provider-render.js'
    provider.write_text(runtime_section('settings/provider_render.js'), encoding='utf-8')
    matrix = tmp_path / 'access-matrix.js'
    matrix.write_text(runtime_section('settings/access_matrix.js'), encoding='utf-8')
    harness = tmp_path / 'harness.js'
    harness.write_text(r'''
const fs = require('fs');
global.window = global;
global.runtimeScope = global;
global.escapeHtml = value => String(value == null ? '' : value)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');
global.t = key => key;
global._detectBrand = () => 'generic';
global._brandSvg = () => '';
global._stgPendingCredentialSecrets = {};
global._stgModelRoutingLoadError = '';
global.document = {
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
};
global.Api = { modelRouting: { probeCellsStatus: () => new Promise(() => {}) } };
global._stgModelRouting = {
  contract_version: 'tofu.model-routing/v2',
  providers: [
    {provider_id: 'p0', name: 'Other', scope: 'owner', brand: 'generic'},
    {provider_id: 'p1', name: 'Gateway', scope: 'owner', brand: 'generic'},
  ],
  provider_accesses: [
    {provider_access_id: 'p0-access', provider_id: 'p0',
      display_name: 'Other', enabled: true, quota_policy: {}},
    {provider_access_id: 'p1-access', provider_id: 'p1',
      display_name: 'Gateway', enabled: true, quota_policy: {}},
  ],
  connections: [
    {connection_id: 'p0-conn', provider_access_id: 'p0-access',
      base_url: 'https://other.example/v1', protocol: 'openai', enabled: true},
    {connection_id: 'p1-conn', provider_access_id: 'p1-access',
      base_url: 'https://gw.example/v1', protocol: 'openai', enabled: true},
  ],
  // p1's two credentials sit at GLOBAL indices 1 and 2, but the matrix
  // numbers its columns 0 and 1 within p1's own access.
  credentials: [
    {credential_id: 'cred-p0', provider_access_id: 'p0-access', kind: 'api_key',
      enabled: true, authorization: {connection_ids: ['p0-conn'], models: []}},
    {credential_id: 'cred-p1a', provider_access_id: 'p1-access', kind: 'api_key',
      enabled: true, authorization: {connection_ids: ['p1-conn'], models: []}},
    {credential_id: 'cred-p1b', provider_access_id: 'p1-access', kind: 'api_key',
      enabled: true, authorization: {connection_ids: ['p1-conn'], models: []}},
  ],
  models: [],
  offerings: [{offering_id: 'off-0', provider_access_id: 'p1-access', identity_state: 'confirmed',
    model: {creator_id: 'google', model_id: 'gemini-3.5-flash'}, enabled: true, stale: false,
    capabilities: ['text'], context_window: 1000}],
  deployments: [{deployment_id: 'dep-0', offering_id: 'off-0', connection_id: 'p1-conn',
    wire_model_id: 'gemini-3.5-flash', enabled: true,
    identity_confidence: 'high', probe_status: 'passed'}],
};
eval(fs.readFileSync(process.argv[2], 'utf8'));
eval(fs.readFileSync(process.argv[3], 'utf8'));
const grants = () => _stgModelRouting.credentials.map(c =>
  (c.authorization.models || []).map(m => m.creator_id + '/' + m.model_id));
const out = {};
_toggleMatrixAccess('p1', 0, 0);   // click column 0 in p1's matrix
out.afterOn = grants();
_toggleMatrixAccess('p1', 0, 1);   // click column 1 as well
out.afterSecond = grants();
_toggleMatrixAccess('p1', 0, 0);   // click column 0 again → toggle back off
out.afterOff = grants();
console.log(JSON.stringify(out));
''', encoding='utf-8')
    output = json.loads(_run_node(harness, provider, matrix).strip().splitlines()[-1])
    # Column 0/1 in p1's matrix are cred-p1a/cred-p1b — the grant must land
    # on those rows, never on the other access's cred-p0 at global index 0.
    assert output['afterOn'] == [[], ['google/gemini-3.5-flash'], []]
    assert output['afterSecond'] == [[], ['google/gemini-3.5-flash'], ['google/gemini-3.5-flash']]
    assert output['afterOff'] == [[], [], ['google/gemini-3.5-flash']]
