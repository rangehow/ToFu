---
name: frontend-settings-decomposition
description: settings.js (4755 LOC) decomposed into static/js/settings/ subpackage of 15 files; settings.js kept as 24-LOC head
enabled: true
tags: [refactor, frontend, javascript, convention]
created: 2026-05-28T07:11:38Z
updated: 2026-05-28T07:11:38Z
---

# `static/js/settings.js` Decomposition (2026-05-28)

Third giant frontend file split. Same recipe as ui.js / main.js
decompositions, with one critical ordering nuance.

## Before

Single 4755-LOC `static/js/settings.js` with:
- L1-16 head: file banner + 3 `var` decls (`_serverConfig`, `_keyStatsCache`, `_keyStatsLoading`)
- L17-128: brand icon helpers
- L129-676: HUGE `_PROVIDER_TEMPLATES` constant (548 LOC of provider catalog data)
- L677-841: Auto-Setup modal (URL+key onboarding probe)
- L842-1448: Local-deployment provider section (endpoints, metrics, discover)
- L1449-1631: settings panel core (switchSettingsTab, openSettings, _loadServerConfig)
- L1632-2018: provider rendering (_renderProvidersTab, _renderModelCard)
- L2019-2241: API key statistics + per-key overrides
- L2242-2454: provider balance check + auto-poll
- L2455-2938: provider template actions (add, sync, discover models)
- L2939-3104: per-model edit/delete (nested in provider card)
- L3105-3412: visibility flags + populate-model-defaults
- L3413-3688: other tabs (Search, Network, MT-test, Feishu, Advanced)
- L3689-3936: save/export/import server config
- L3937-4296: OAuth flows
- L4297-4755: MCP catalog + install modal

## After

`settings.js` reduced from 4755 → 24 LOC: just the head (L1-16 banner
+ 3 var decls) + an 8-line pointer comment. The body extracted into
15 cohesive sibling files:

```
static/js/settings/
  branding.js              112 LOC — brand icons, _brandSvg, _detectBrand, _modelShortName
  provider_templates.js    548 LOC — _PROVIDER_TEMPLATES const + external loader
  auto_setup.js            165 LOC — _showAutoSetupModal + _runAutoProbe + _showAutoStatus
  local_endpoints.js       607 LOC — local providers (per-endpoint metrics, status, discovery)
  core_panel.js            183 LOC — switchSettingsTab, _loadServerConfig, openSettings
  provider_render.js       387 LOC — _renderProvidersTab, _renderModelCard
  key_stats.js             223 LOC — API key stats + per-key enable/disable overrides
  balance.js               213 LOC — provider balance check + auto-poll + badge
  template_actions.js      484 LOC — add/del provider, _showTemplateMenu, _syncFromTemplate, _discoverModels
  model_edit.js            166 LOC — _addModel, _deleteModel, _editModel, _saveModelEdit, alias mgmt
  visibility_defaults.js   308 LOC — _renderIgVisibility, _renderDropdownVisibility, _populateModelDefaults
  other_tabs.js            276 LOC — Search/Network/MT/Feishu/Advanced tabs + cache stats
  save_export.js           248 LOC — saveSettings, _saveServerConfig, export/import
  oauth.js                 360 LOC — OAuth status/login/logout/manual-callback
  mcp.js                   459 LOC — MCP catalog UI, install modal, save server
```

Total extracted: 4739 LOC. Slim settings.js: 24 LOC. Sum: 4763
(vs 4755 + 8-line pointer = consistent).

## CRITICAL ordering: settings.js BEFORE settings/

Unlike main.js (which boots LAST), settings.js's HEAD declares globals
(`var _serverConfig`, `var _keyStatsCache`, etc.) that the extracted
files mutate. The `var` initialiser MUST run BEFORE the assignments in
the subpackage, otherwise the head's `= null` would clobber any
in-progress state. The bundler order is therefore:

```python
'myday.js',
'settings.js',                          # ★ 24-LOC head WITH var decls
'settings/branding.js',                 # ── subpackage starts ──
'settings/provider_templates.js',
... (13 more files) ...
'settings/mcp.js',
'api-keys.js',
```

`index.html` mirrors this order.

## Pure source split — body byte-equivalent

Every code line is unchanged. Only added text:
- 15 × 10-line banner comments at the top of each extracted file
- 1 × 8-line pointer comment inside settings.js

## Verification

- All 15 extracted files: `node -c` clean.
- New (slim) settings.js: `node -c` clean.
- Bundler builds (`bundle-1495e0a6.js`).
- 37 sampled symbols verified present in the bundle:
  `openSettings`, `closeSettings`, `saveSettings`, `switchSettingsTab`,
  `_renderProvidersTab`, `_renderModelCard`, `_populateModelDropdown`,
  `addLocalProvider`, `_discoverLocalModels`, `_checkProviderBalance`,
  `_showAutoSetupModal`, `_runAutoProbe`, `_showTemplateMenu`,
  `addProviderFromTemplate`, `_syncFromTemplate`, `_discoverModels`,
  `_editModel`, `_saveModelEdit`, `_renderIgVisibility`,
  `_renderDropdownVisibility`, `_populateModelDefaults`,
  `_populateNetworkTab`, `_populateMtProviderSection`, `_testMtProvider`,
  `_populateFeishuTab`, `_saveServerConfig`, `exportServerConfig`,
  `importServerConfig`, `_loadOAuthStatus`, `_oauthLogin`, `_oauthLogout`,
  `_oauthManualSubmit`, `_populateMcpTab`, `_renderMcpCatalog`,
  `_mcpQuickInstall`, `_mcpDoInstall`, `_mcpSaveServer`.
- API isolation 4/4 pass; backend tests 86/86; translate 10/10; paper 14/14.

## Boundary lessons

- The `/**` doc-comment for `_checkProviderBalance` straddled my
  initial split (key_stats.js ended mid-comment, balance.js started
  with `*/`). Fixed by moving boundary to L2241/L2242 (after the
  blank line preceding the doc).
- Same straddle on auto_setup → local_endpoints (the 8-line `// ══`
  banner intro to local_endpoints landed in auto_setup). Fixed by
  moving boundary to L841/L842.
- `_offerTemplateUpdate` is conceptually part of template_actions
  (not model_edit), even though it appears AFTER `_addModel` in
  source order. Boundary chosen so `_offerTemplateUpdate` is the
  last function in template_actions.js (L2920-2937).

## Next decompositions

| File | Current LOC | Pattern |
|---|---|---|
| `static/js/core.js` | 3919 | network + state + markdown + folders + IDB cache |

