#!/usr/bin/env python3
"""jsdom test for the Network settings tab proxy pool editor
(settings/other_tabs.js + settings/save_export.js, real core/safe_html.js).

Pins the contracts the 2026-08-07 pool feature introduced:

  * rows render from cfg.network.proxy_pool (url/name/scope/enabled);
    a saved credential shows the "留空不变" placeholder, never a value.
  * legacy migration: a configured legacy single proxy with no global pool
    row surfaces as ONE synthetic global row (saving migrates it).
  * add/delete row interactions; blank-URL rows are dropped on collect.
  * _collectProxyPool returns null when the editor is absent (other
    surfaces must not clobber the server's proxy config).
  * save payload carries proxy_pool (and no legacy proxy_config).
  * the per-row 测试 button renders per-target verdicts (ok / geo_blocked /
    network_fail) and the error path.

Run: make test-frontend  (skips cleanly when node/jsdom aren't installed)
"""

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' +
    '<div id="proxyPoolList"></div>' +
    '<button id="proxyPoolAddBtn"></button>' +
    '<div id="proxyEnvBanner" style="display:none"><span id="proxyEnvBannerText"></span></div>' +
    '<div id="settingProxyBypass"></div>' +
    '<div id="proxyEnvHint" style="display:none"><span id="proxyEnvHintText"></span></div>' +
    '</body>',
  targets: [process.argv[4], process.argv[2], process.argv[5]],
  globals: {
    _setVal: function (id, value) {
      var el = document.getElementById(id);
      if (el) el.value = value;
    },
    t: function (key) {
      var dict = {
        'settings.proxyPoolAdd': '添加代理',
        'settings.proxyPhName': '名称',
        'settings.proxyPhCred': 'user:password（可选）',
        'settings.proxyCredSaved': '凭证已保存（留空不变）',
        'settings.proxyScopeSub': '仅订阅流量',
        'settings.proxyScopeGlobal': '全局流量',
        'settings.proxyEnable': '启用',
        'settings.proxyTest': '测试',
        'settings.proxyTesting': '测试中…',
        'settings.proxyDel': '删除',
        'settings.proxyTestOkTpl': '{label} 可达（HTTP {code} · {ms}ms）',
        'settings.proxyTestBlockedTpl': '{label} 被拦截（HTTP {code}）',
        'settings.proxyTestFailTpl': '{label} 网络失败：{err}',
        'settings.proxyLegacyRow': '旧版单代理（迁移）',
      };
      return dict[key] || '';
    },
    ChipInput: {
      init: function (id, vals) { this._vals = vals || []; },
      getValues: function (id) { return this._vals || []; },
    },
    _stgPresets: {},
    _stgProviders: [],
    _serverConfig: { hidden_models: [], hidden_ig_models: [] },
    _collectModelDefaults: function () { return {}; },
    Api: {
      serverConfig: { update: async function (payload) {
        window.__savedPayload = payload;
        return { json: async () => ({ ok: true }) };
      } },
      network: { proxyTest: async function (payload) {
        window.__testPayload = payload;
        if (payload.url.indexOf('broken') !== -1) throw new Error('boom');
        return { any_ok: true, results: [
          { label: 'OpenAI Auth', target: 'https://auth.openai.com/oauth/token',
            status: 400, latency_ms: 321, verdict: 'ok' },
          { label: 'Anthropic API', target: 'https://api.anthropic.com/v1/messages',
            status: 403, latency_ms: 100, verdict: 'geo_blocked' },
        ] };
      } },
    },
    _loadServerConfigAndPopulate: function () {},
    debugLog: function () {},
  },
});

const $ = (id) => document.getElementById(id);
const rows = () => document.querySelectorAll('#proxyPoolList .proxy-pool-row');

(async () => {
  try {
    const cfg = {
      network: {
        http_proxy: '', https_proxy: '', proxy_configured: false,
        proxy_bypass_domains: ['.corp.example'],
        proxy_pool: [
          { id: 'hk', name: 'HK 网关', url: 'http://g-hk.example.com:8080',
            scope: 'subscription', enabled: true,
            has_credential: true, credential_vault: 'proxy_hk_auth' },
          { id: 'plain', name: '', url: 'http://plain.example.com:3128',
            scope: 'global', enabled: false,
            has_credential: false, credential_vault: '' },
        ],
      },
    };

    // ── 1. rows render from config ──
    _populateNetworkTab(cfg);
    check('two_rows_rendered', rows().length === 2);
    check('row_url_populated',
      rows()[0].querySelector('.pp-url').value === 'http://g-hk.example.com:8080');
    check('row_scope_populated',
      rows()[0].querySelector('.pp-scope').value === 'subscription');
    check('row_enabled_checked',
      rows()[0].querySelector('.pp-enabled').checked === true);
    check('row2_disabled_unchecked',
      rows()[1].querySelector('.pp-enabled').checked === false);
    check('saved_credential_placeholder_never_value',
      rows()[0].querySelector('.pp-cred').placeholder.indexOf('留空不变') !== -1 &&
      rows()[0].querySelector('.pp-cred').value === '');
    check('vault_ref_hidden_input',
      rows()[0].querySelector('.pp-credvault').value === 'proxy_hk_auth');

    // ── 2. no legacy synthetic row when pool already covers global ──
    check('no_legacy_row_when_global_exists',
      !document.querySelector('.proxy-pool-row[data-id="legacy"]'));

    // ── 3. legacy migration row synthesized ──
    _populateNetworkTab({ network: {
      http_proxy: 'http://old.example.com:3128', proxy_configured: true,
      proxy_pool: [{ id: 'hk', name: '', url: 'http://g-hk.example.com:8080',
                     scope: 'subscription', enabled: true,
                     has_credential: true, credential_vault: 'proxy_hk_auth' }],
    } });
    const legacyRow = document.querySelector('.proxy-pool-row[data-id="legacy"]');
    check('legacy_row_synthesized', !!legacyRow);
    check('legacy_row_is_global',
      legacyRow && legacyRow.querySelector('.pp-scope').value === 'global');

    // ── 4. add / delete ──
    const before = rows().length;
    $('proxyPoolAddBtn').click();
    check('add_appends_row', rows().length === before + 1);
    const newRow = rows()[rows().length - 1];
    newRow.querySelector('.pp-url').value = 'http://new.example.com:8080';
    newRow.querySelector('.pp-name').value = '新代理';
    newRow.querySelector('.pp-cred').value = 'u:p';
    // jsdom inline-attr handlers resolve against win, not node global —
    // call the implementation directly with the element ref instead.
    _proxyPoolDelete(newRow.querySelector('button.icon-box'));
    check('delete_removes_row', rows().length === before);

    // ── 5. collect: blank rows dropped, fields carried ──
    $('proxyPoolAddBtn').click();  // blank row — must be dropped
    const collected = _collectProxyPool();
    check('collect_drops_blank_rows', collected.length === 2);  // hk + legacy
    check('collect_carries_fields',
      collected[0].url === 'http://g-hk.example.com:8080' &&
      collected[0].scope === 'subscription' &&
      collected[0].enabled === true &&
      collected[0].credential_vault === 'proxy_hk_auth');

    // ── 6. save payload carries proxy_pool, no legacy proxy_config ──
    await _saveServerConfig();
    check('save_payload_has_pool',
      Array.isArray(window.__savedPayload.proxy_pool) &&
      window.__savedPayload.proxy_pool.length === 2);
    check('save_payload_no_legacy_proxy_config',
      !('proxy_config' in window.__savedPayload));

    // ── 7. test button: verdicts rendered per target ──
    const row = rows()[0];
    row.querySelector('.pp-cred').value = 'fresh:pw';
    _proxyPoolTest(row.querySelectorAll('button.auth-src-btn')[0]);  // 测试
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    const resultEl = row.querySelector('.pp-result');
    check('test_payload_carried_credential',
      window.__testPayload && window.__testPayload.credential === 'fresh:pw');
    check('test_result_reachable_line',
      resultEl.textContent.indexOf('OpenAI Auth 可达（HTTP 400 · 321ms）') !== -1);
    check('test_result_blocked_line',
      resultEl.textContent.indexOf('Anthropic API 被拦截（HTTP 403）') !== -1);
    check('test_result_ok_class', resultEl.className.indexOf('ok') !== -1);

    // ── 8. test error path ──
    row.querySelector('.pp-url').value = 'http://broken.example.com:8080';
    _proxyPoolTest(row.querySelectorAll('button.auth-src-btn')[0]);
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    check('test_error_path_err_class',
      resultEl.className.indexOf('err') !== -1 &&
      resultEl.textContent.indexOf('boom') !== -1);

    // ── 9. container absent → null (other surfaces untouched) ──
    const list = $('proxyPoolList');
    list.parentNode.removeChild(list);
    check('collect_null_without_container', _collectProxyPool() === null);
  } catch (e) {
    check('harness_threw: ' + (e && e.message), false);
  } finally {
    report();
  }
})();
'''


def test_proxy_pool_editor_frontend():
    run_harness(
        target_js=os.path.join(JS_DIR, 'settings', 'other_tabs.js'),
        body_js=_BODY,
        extra_targets=[
            os.path.join(JS_DIR, 'core', 'safe_html.js'),
            os.path.join(JS_DIR, 'settings', 'save_export.js'),
        ],
        expect_pass=22,
        timeout=300,
        label='proxy-pool-editor',
    )
