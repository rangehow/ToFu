const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const mod = {exports: {}};
new Function('module', 'exports', ts.transpileModule(fs.readFileSync(
  'frontend/src/features/settings/provider-connection-staging.ts', 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
}).outputText)(mod, mod.exports);
const {resolveProviderFaceConnection, authorizeProviderConnection} = mod.exports;
const ports = {newId: prefix => prefix + '-new', disabledConnection: row => new Error('disabled:' + row.connection_id)};

test('face preparation fills only missing routing metadata without editing existing rows', () => {
  const row = {connection_id: 'existing', provider_access_id: 'a', base_url: 'https://synthetic.invalid/v1/',
    protocol: 'anthropic', enabled: true, responses_profile: 'codex',
    inference_profile: {reasoning: {support: 'unsupported'}}, extra_headers: {'x-owner': 'custom'}};
  const before = structuredClone(row);
  const result = resolveProviderFaceConnection([row], 'a', {
    base_url: 'https://synthetic.invalid/v1', protocol: 'openai', face: 'bedrock',
    responses_profile: 'compatible', inference_profile: {reasoning: {support: 'supported'}},
    extra_headers: {'x-owner': 'template', 'x-selector': 'required'},
  }, ports);
  assert.equal(result.previous, row);
  assert.equal(result.connection.connection_id, 'existing');
  assert.equal(result.connection.protocol, 'anthropic');
  assert.equal(result.connection.responses_profile, 'codex');
  assert.equal(result.connection.inference_profile, row.inference_profile);
  assert.equal(result.connection.gateway_namespace, 'bedrock');
  assert.deepEqual(result.connection.extra_headers, {'x-owner': 'custom', 'x-selector': 'required'});
  assert.deepEqual(row, before);
});

test('face resolution is access scoped, rejects disabled matches and does not publish new rows', () => {
  const foreign = {connection_id: 'foreign', provider_access_id: 'other', base_url: 'https://synthetic.invalid', enabled: false};
  const plan = {base_url: 'https://synthetic.invalid/', extra_headers: {'x-selector': 'required'}};
  const rows = [foreign];
  const result = resolveProviderFaceConnection(rows, 'a', plan, ports);
  assert.equal(result.previous, null);
  assert.equal(result.connection.connection_id, 'conn-new');
  assert.equal(result.connection.provider_access_id, 'a');
  assert.equal(result.connection.protocol, 'openai');
  assert.equal(result.connection.enabled, true);
  assert.deepEqual(rows, [foreign]);
  const disabled = {...foreign, provider_access_id: 'a'};
  assert.throws(() => resolveProviderFaceConnection([disabled], 'a', plan, ports), /disabled:foreign/);
  assert.equal(resolveProviderFaceConnection(rows, 'a', undefined, ports), null);
});

test('same URL never substitutes one named selector face for another', () => {
  const kunlun = {connection_id: 'kunlun', provider_access_id: 'a',
    base_url: 'https://synthetic.invalid/v1', gateway_namespace: 'kunlun',
    protocol: 'openai_responses', enabled: true,
    extra_headers: {'x-context-provider': 'kunlun'}};
  const tokrouter = {...kunlun, connection_id: 'tokrouter', gateway_namespace: 'tokrouter',
    extra_headers: {'x-context-provider': 'tokrouter'}};
  const plan = {base_url: 'https://synthetic.invalid/v1', protocol: 'openai_responses',
    face: 'tokrouter', extra_headers: {'x-context-provider': 'tokrouter'}};
  const result = resolveProviderFaceConnection([kunlun, tokrouter], 'a', plan, ports);
  assert.equal(result.previous, tokrouter);
  assert.equal(result.connection.connection_id, 'tokrouter');
  assert.deepEqual(result.connection.extra_headers, {'x-context-provider': 'tokrouter'});
  assert.deepEqual(kunlun.extra_headers, {'x-context-provider': 'kunlun'});
});

test('connection grants preserve existing model denials, metadata and untouched identities', () => {
  const authorization = {connection_ids: ['existing'], models: [
    {creator_id: 'creator', model_id: 'model', excluded_wire_model_ids: ['denied']}], extra_policy: {keep: true}};
  const before = structuredClone(authorization);
  assert.equal(authorizeProviderConnection(authorization, 'existing'), authorization);
  const extended = authorizeProviderConnection(authorization, 'new');
  assert.deepEqual(extended.connection_ids, ['existing', 'new']);
  assert.equal(extended.models, authorization.models);
  assert.equal(extended.extra_policy, authorization.extra_policy);
  assert.deepEqual(authorization, before);
  assert.deepEqual(authorizeProviderConnection(undefined, 'new'), {connection_ids: ['new'], models: []});
});

for (const enabled of [true, false]) {
  test(`exact selector outranks earlier generic same-URL connection (enabled=${enabled})`, () => {
    const generic = {connection_id: 'generic', provider_access_id: 'a',
      base_url: 'https://synthetic.invalid/v1', enabled: true,
      extra_headers: {'x-context-provider': 'kunlun'}};
    const exact = {...generic, connection_id: 'tokrouter', gateway_namespace: 'tokrouter',
      enabled, extra_headers: {'x-context-provider': 'tokrouter'}};
    const before = structuredClone([generic, exact]);
    const plan = {base_url: generic.base_url, face: 'tokrouter',
      protocol: 'openai_responses', extra_headers: {'x-context-provider': 'tokrouter'}};
    if (enabled) {
      const result = resolveProviderFaceConnection([generic, exact], 'a', plan, ports);
      assert.equal(result.previous, exact);
      assert.deepEqual(result.connection.extra_headers, exact.extra_headers);
    } else {
      assert.throws(() => resolveProviderFaceConnection([generic, exact], 'a', plan, ports), /disabled:tokrouter/);
    }
    assert.deepEqual([generic, exact], before);
  });
}
