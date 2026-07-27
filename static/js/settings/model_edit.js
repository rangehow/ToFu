/* ═══════════════════════════════════════════════════════════════════
   settings/model edit — extracted from settings.js (split 2026-05-28)

   Per-model edit/delete + alias management (nested inside provider card).

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ── Model CRUD (nested inside provider) ──

function _addModel(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  if (!p.models) p.models = [];
  p.models.push({
    model_id: '', aliases: [], capabilities: ['text'], rpm: 30, cost: 0.01, thinking_default: false
  });
  _renderProvidersTab();
  // Open edit for the new model
  _editModel(provIdx, p.models.length - 1);
  // Make sure provider is expanded
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (card) card.classList.add('expanded');
}

async function _deleteModel(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models || !p.models[modelIdx]) return;
  var mid = p.models[modelIdx].model_id;
  if (!await showConfirm(t('settings.meDeleteConfirm', { name: (mid || t('settings.meUnnamed')) }), { danger: true })) return;
  p.models.splice(modelIdx, 1);
  // Clear presets pointing to this model
  for (var k in _stgPresets) {
    if (_stgPresets[k] === mid) _stgPresets[k] = '';
  }
  _renderProvidersTab();
  _renderPresetsTab(_serverConfig);
}

/** Toggle the 'thinking' capability + thinking_default on a model card.
 *
 * Auto-discovery's regex only flags Qwen 3+, GLM 4.5+, DeepSeek V4+, Claude/o1/o3,
 * etc. as thinking models. Self-hosted custom builds (Qwen 2.5 finetunes, GLM 4.5
 * forks renamed, in-house reasoning models) won't match — this toggle lets the
 * user opt in without entering the edit form.
 */
function _toggleModelThinking(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m) return;
  var caps = (m.capabilities || []).slice();
  var idx = caps.indexOf('thinking');
  if (idx >= 0) {
    caps.splice(idx, 1);
    m.thinking_default = false;
  } else {
    caps.push('thinking');
    m.thinking_default = true;
  }
  m.capabilities = caps;
  _renderProvidersTab();
}

/** Toggle the per-model enabled flag.
 *
 * A disabled model is excluded from slot-pool construction
 * (lib/llm_dispatch/dispatcher.py) AND from dropdown_models
 * (routes/config.py), so it disappears from the chat picker and
 * the dispatcher will never route to it. The model entry itself
 * stays in server_config.json so the user can re-enable it later
 * without losing aliases / pricing / RPM settings.
 */
function _toggleModelEnabled(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m) return;
  m.enabled = (m.enabled === false);
  _renderProvidersTab();
}

function _editModel(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m) return;

  // Find the model card and insert edit form after it
  var card = document.querySelector('.stg-mcard[data-prov="' + provIdx + '"][data-model="' + modelIdx + '"]');
  if (!card) return;
  // Remove any existing edit forms
  var existing = card.parentElement.querySelector('.stg-edit-form');
  if (existing) existing.remove();

  var allCaps = ['text', 'vision', 'video', 'thinking', 'cheap', 'image_gen', 'embedding', 'transcription', 'audio_chat'];
  var html = '<div class="stg-edit-form">';
  html += '<div class="stg-edit-grid">' +
    '<div class="stg-field"><label>' + escapeHtml(t('settings.meModelId')) + '</label>' +
      '<input type="text" class="stg-edit-mid" value="' + escapeHtml(m.model_id || '') + '" placeholder="' + escapeHtml(t('settings.meModelIdPlaceholder')) + '"></div>' +
    '<div class="stg-field"><label>' + escapeHtml(t('settings.meRpm')) + '</label>' +
      '<input type="number" class="stg-edit-rpm" value="' + (m.rpm || 30) + '" min="1"></div>' +
    '<div class="stg-field"><label>' + escapeHtml(t('settings.meCost')) + ' <span class="stg-hint">' + escapeHtml(t('settings.meCostHint')) + '</span></label>' +
      '<input type="number" class="stg-edit-cost" value="' + (m.cost || 0.01) + '" step="0.001" min="0"></div>' +
  '</div>';

  html += '<div class="stg-field"><label>' + escapeHtml(t('settings.meCapabilities')) + '</label><div class="stg-cap-toggles">';
  for (var ci = 0; ci < allCaps.length; ci++) {
    var cap = allCaps[ci];
    var active = (m.capabilities || []).indexOf(cap) >= 0;
    html += '<button type="button" class="stg-cap-btn' + (active ? ' active' : '') + '" data-cap="' + cap + '" onclick="this.classList.toggle(\'active\')">' + cap + '</button>';
  }
  html += '</div></div>';

  var _field = _poolField(m);
  var _isPool = (_field === 'request_ids');
  html += '<div class="stg-field"><label>' +
    escapeHtml(t(_isPool ? 'settings.meRequestIds' : 'settings.meAliases')) +
    ' <span class="stg-hint">' +
    escapeHtml(t(_isPool ? 'settings.meRequestIdsHint' : 'settings.meAliasesHint')) +
    '</span></label>' +
    '<input type="text" class="stg-edit-aliases" data-pool-field="' + _field + '"' +
    ' value="' + escapeHtml((m[_field] || []).join(', ')) + '"' +
    ' placeholder="' + escapeHtml(t(_isPool
      ? 'settings.meRequestIdsPlaceholder' : 'settings.meAliasesPlaceholder')) + '"></div>';

  html += '<div class="stg-toggle-row"><span>' + escapeHtml(t('settings.meThinkingDefault')) + '</span>' +
    '<label class="stg-toggle"><input type="checkbox" class="stg-edit-think"' + (m.thinking_default ? ' checked' : '') + '>' +
    '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span></label></div>';

  html += '<div class="stg-edit-actions">' +
    '<button class="stg-btn-secondary" onclick="this.closest(\'.stg-edit-form\').remove()">' + escapeHtml(t('settings.cancel')) + '</button>' +
    '<button class="stg-btn-primary" onclick="_saveModelEdit(' + provIdx + ',' + modelIdx + ')">' + escapeHtml(t('settings.apply')) + '</button>' +
  '</div>';
  html += '</div>';

  card.insertAdjacentHTML('afterend', html);
  // Focus model ID input
  var midInput = card.nextElementSibling.querySelector('.stg-edit-mid');
  if (midInput && !midInput.value) { midInput.focus(); }
}

function _saveModelEdit(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m) return;

  var form = document.querySelector('.stg-edit-form');
  if (!form) return;

  var oldModelId = m.model_id;
  m.model_id = String(form.querySelector('.stg-edit-mid').value || '').trim();
  m.rpm = parseInt(form.querySelector('.stg-edit-rpm').value) || 30;
  m.cost = parseFloat(form.querySelector('.stg-edit-cost').value) || 0.01;
  m.thinking_default = form.querySelector('.stg-edit-think').checked;

  var caps = [];
  form.querySelectorAll('.stg-cap-btn.active').forEach(function(el) { caps.push(el.dataset.cap); });
  m.capabilities = caps;

  var _aliasEl = form.querySelector('.stg-edit-aliases');
  var _saveField = (_aliasEl && _aliasEl.dataset.poolField) || 'aliases';
  var aliasStr = (_aliasEl ? _aliasEl.value || '' : '').trim();
  var _parsed = aliasStr
    ? aliasStr.split(',').map(function(s) { return s.trim(); }).filter(Boolean)
    : [];
  // Never let an edit empty a wire pool — that resolves to zero slots and the
  // model disappears from routing while still rendering in the card.
  if (_saveField === 'request_ids' && !_parsed.length) {
    showAlert(t('settings.meRequestIdsLastWarn'));
    return;
  }
  m[_saveField] = _parsed;

  // Update presets if model_id changed
  if (oldModelId && oldModelId !== m.model_id) {
    for (var k in _stgPresets) {
      if (_stgPresets[k] === oldModelId) _stgPresets[k] = m.model_id;
    }
  }

  // Re-position this model alphabetically: the id may have just been set
  // (new model) or changed (rename). Pull it out and re-insert in order.
  if (typeof _insertModelSorted === 'function') {
    p.models.splice(modelIdx, 1);
    _insertModelSorted(p.models, m);
  }

  _renderProvidersTab();
  _renderPresetsTab(_serverConfig);
}

// ── Wire-id pool CRUD (from model card chips) ──
//
// The card renders `request_ids` when the entry declares one (the wire pool of
// the model-identity contract) and falls back to legacy `aliases` otherwise.
// These handlers MUST mutate whichever field the card actually rendered, or a
// chip removal is a no-op that looks like it worked.

/** Name of the field backing the chips for this entry. */
function _poolField(m) {
  return (m && m.request_ids && m.request_ids.length) ? 'request_ids' : 'aliases';
}

async function _addAlias(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m) return;
  var field = _poolField(m);
  var alias = String(await showPrompt(t(field === 'request_ids'
    ? 'settings.meRequestIdPrompt' : 'settings.meAliasPrompt')) || '');
  if (!alias || !alias.trim()) return;
  if (!m[field]) m[field] = [];
  alias = alias.trim();
  // A wire pool MAY legitimately contain the logical model_id (an entry whose
  // logical name the gateway also accepts); legacy aliases may not, since the
  // root is implicitly in that pool already.
  var clash = (field === 'aliases') && alias === m.model_id;
  if (m[field].indexOf(alias) === -1 && !clash) {
    m[field].push(alias);
    _renderProvidersTab();
  }
}

function _removeAlias(provIdx, modelIdx, aliasIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m) return;
  var field = _poolField(m);
  if (!m[field]) return;
  // Refuse to empty a wire pool: an entry with no request id resolves to zero
  // slots, so the model silently vanishes from routing while still showing in
  // the card. Deleting the model is the explicit way to do that.
  if (field === 'request_ids' && m[field].length <= 1) {
    showAlert(t('settings.meRequestIdsLastWarn'));
    return;
  }
  m[field].splice(aliasIdx, 1);
  _renderProvidersTab();
}

