/* ===== migrated source: settings/model_catalog_tab.js ===== */
/*
 * 模型 (Models) settings tab — the global, read-only Creator/Model registry.
 *
 * Responsibility: own everything rendered into ``#settingsTab_models``
 * (static/settings_panels/models.html). The page answers exactly one
 * question: which official models exist, grouped by model developer
 * (Creator). Provider supply, credentials, offerings, deployments, aliases,
 * and routes never render here — the projection built below is a real data
 * boundary, not a view convention: the typed Model feature
 * (frontend/src/features/model-catalog/) receives only Creator/Model fields
 * even though the staged authority document holds the rest.
 *
 * The 服务商 (Providers) tab is the mirror image: settings/provider_render.js
 * owns provider supply and never re-renders this page. Flows that reload the
 * v2 authority or mutate creators/models call ``_renderModelRoutingTabs()``
 * so both pages repaint by name, not as a side effect.
 */

/** Search text staged by the page's search box (data-tofu-action-input in
 *  static/settings_panels/models.html). Kept across re-renders so typing in
 *  either tab never drops the query. */
let _stgModelCatalogQuery = '';

/** Repaint both model-routing settings tabs. Use this after any flow that
 *  reloads the v2 authority or changes creators/models (settings open,
 *  subscription sync, template bundle add, provider delete). Pure
 *  provider-side edits (credentials, connections, offerings, matrix grants)
 *  call ``_renderProvidersTab()`` alone — the registry does not change. */
function _renderModelRoutingTabs() {
  _renderProvidersTab();
  _renderModelCatalogTab();
}

function _setModelCatalogSearch(value) {
  var owner = runtimeScope._setModelCatalogSearchOwner;
  if (typeof owner === 'function') return owner(value);
  _stgModelCatalogQuery = String(value || '');
  _renderModelCatalogTab();
}

function _renderModelCatalogTab() {
  var list = document.getElementById('stgModelCatalog');
  if (!list) return;
  if (!_stgModelRouting) {
    list.innerHTML = '<p class="stg-empty">' + escapeHtml(_stgModelRoutingLoadError
      ? '加载模型目录失败：' + _stgModelRoutingLoadError : '正在加载模型目录…') + '</p>';
    return;
  }
  // This is an actual data boundary, not just a view convention: the Model
  // feature receives a fresh Creator/Model-only projection, so provider-side
  // fields are unavailable to it even if the authority document contains them.
  var catalogDocument = {
    contract_version: _stgModelRouting.contract_version,
    creators: (_stgModelRouting.creators || []).map(function(creator) {
      return { creator_id: creator.creator_id, name: creator.name };
    }),
    models: (_stgModelRouting.models || []).map(function(model) {
      // Derived vendor knowledge (release_date, official_pricing) is stamped
      // by the backend at the read boundary; forward both verbatim so the
      // typed feature can fall back to the official price table when the
      // row carries no registered list_pricing.
      var pricing = model.list_pricing;
      var official = model.official_pricing;
      var copyPricing = function(source) {
        return source ? {
          input: source.input,
          output: source.output,
          currency: source.currency,
          unit: source.unit,
          cache_read: source.cache_read,
          cache_write: source.cache_write,
        } : undefined;
      };
      return {
        creator_id: model.creator_id,
        model_id: model.model_id,
        display_name: model.display_name,
        capabilities: (model.capabilities || []).slice(),
        context_window: model.context_window,
        quality_rank: model.quality_rank,
        release_date: model.release_date || undefined,
        list_pricing: copyPricing(pricing),
        official_pricing: copyPricing(official),
        lifecycle: model.lifecycle,
      };
    }),
  };
  var owner = runtimeScope._renderModelCatalogPanel;
  if (typeof owner === 'function') {
    owner(catalogDocument);
    return;
  }
  // The typed owner may be absent in an embedded/test shell. Its fallback is
  // still Model-only: never infer model facts from provider supply or aliases.
  var documentValue = catalogDocument;
  var query = _stgModelCatalogQuery.trim().toLowerCase();
  var rows = (documentValue.models || []).filter(function(model) {
    var haystack = [model.display_name, model.model_id, model.creator_id].join(' ').toLowerCase();
    return !query || haystack.includes(query);
  }).sort(function(left, right) {
    return String(left.display_name || left.model_id).localeCompare(
      String(right.display_name || right.model_id), undefined,
      { numeric: true, sensitivity: 'base' });
  });
  var visible = rows.slice(0, 120);
  var input = document.getElementById('stgModelCatalogSearch');
  if (input && input.value !== _stgModelCatalogQuery) input.value = _stgModelCatalogQuery;
  list.innerHTML = '<div class="stg-v2-catalog-count">' + rows.length + ' 个官方模型' +
    (query ? ' 匹配当前搜索' : '') + '</div><div class="stg-v2-catalog-list">' +
    visible.map(function(model) {
      return '<article class="stg-v2-catalog-row"><div class="stg-v2-catalog-icon">' +
        _brandSvg(typeof _modelBrand === 'function'
          ? _modelBrand(model.model_id || '', model.creator_id)
          : _detectBrand((model.creator_id || '') + ' ' + (model.model_id || '')), 18) + '</div>' +
        '<div class="stg-v2-catalog-identity"><strong>' + escapeHtml(model.display_name || model.model_id) +
        '</strong><span>' + escapeHtml(model.creator_id + '/' + model.model_id) + '</span></div></article>';
    }).join('') + '</div>';
}
