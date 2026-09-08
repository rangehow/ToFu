/* ===== migrated source: settings/local_deploy.js ===== */
/*
 * Local deployment Settings projection.
 *
 * Responsibility: the dedicated 本地部署 page —
 *   1. 智能推荐: live hardware probe + curated catalog ranked against it
 *      (read-only /api/v1/local-serve/*), each card handing off to the chat
 *      agent for download + managed deploy;
 *   2. 云端替代: DeepSeek V4 Flash escape hatch when hardware is too weak,
 *      deep-linking the DeepSeek provider template wizard;
 *   3. 托管实例: ledger rows with live status; failed rows hand off to the
 *      agent's diagnosis/repair flow;
 *   4. 手动接入: engine preset endpoint probe-and-stage, managed model-path
 *      handoff, and the fully custom provider path.
 * Staging authority stays with provider_render.js (_stageModelRoutingProviderBundle);
 * all deploy/stop/remove writes stay with the approval-gated local_serve agent tools.
 */

var _LOCAL_DEPLOY_PRESETS = [
  { engine: 'vllm', icon: 'vllm', name: 'vLLM',
    placeholder: 'http://10.0.0.5:8000/v1',
    descKey: 'settings.localPresetVllmDesc' },
  { engine: 'sglang', icon: 'sglang', name: 'SGLang',
    placeholder: 'http://10.0.0.5:30000/v1',
    descKey: 'settings.localPresetSglangDesc' },
  { engine: 'ollama', icon: 'ollama', name: 'Ollama',
    placeholder: 'http://localhost:11434/v1',
    descKey: 'settings.localPresetOllamaDesc' },
  { engine: 'llamacpp', icon: 'llamacpp', name: 'llama.cpp',
    placeholder: 'http://localhost:8080/v1',
    descKey: 'settings.localPresetLlamacppDesc' },
  { engine: 'managed', icon: 'local',
    nameKey: 'settings.localPresetManagedName',
    descKey: 'settings.localPresetManagedDesc' },
  // Custom comes LAST (owner-ratified 2026-07-25).
  { engine: '', icon: 'local', custom: true,
    nameKey: 'settings.localPresetCustomName',
    descKey: 'settings.localPresetCustomDesc' },
];

var _LD_PURPOSES = ['', 'chat', 'coding', 'reasoning', 'vision', 'multilingual'];

var _LD_TIER_CLASS = {
  comfortable: 'is-comfortable', tight: 'is-tight',
  extreme: 'is-extreme', cpu: 'is-cpu',
};

var _LD_TIER_LABEL_KEY = {
  comfortable: 'settings.localRecFitComfortable',
  tight: 'settings.localRecFitTight',
  extreme: 'settings.localRecFitExtreme',
  cpu: 'settings.localRecFitCpu',
};

var _LD_STATUS_LABEL_KEY = {
  planned: 'settings.localStatusPlanned',
  installing: 'settings.localStatusInstalling',
  starting: 'settings.localStatusStarting',
  running: 'settings.localStatusRunning',
  stopped: 'settings.localStatusStopped',
  failed: 'settings.localStatusFailed',
};

function _localDeployWireClose(overlay, modal) {
  function close() { overlay.remove(); }
  overlay.onclick = function(event) { if (event.target === overlay) close(); };
  modal.querySelector('.stg-modal-close').onclick = close;
  return close;
}

function _localDeployPresetTile(preset, overlay) {
  var item = document.createElement('button');
  item.type = 'button';
  item.className = 'stg-template-item stg-local-preset';
  item.innerHTML = '<span class="stg-local-preset-icon">' +
    _brandSvg(preset.icon, 22) + '</span>' +
    '<span class="stg-template-info"><span class="stg-template-name">' +
    escapeHtml(preset.nameKey ? t(preset.nameKey) : preset.name) +
    '</span><span class="stg-template-models">' +
    escapeHtml(t(preset.descKey)) + '</span></span>' +
    '<span class="stg-local-preset-arrow"><svg viewBox="0 0 16 16" width="14" height="14" ' +
    'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round"><path d="M6 3.5 10.5 8 6 12.5"/></svg></span>';
  item.onclick = function() {
    overlay.remove();
    if (preset.engine === 'managed') _openManagedDeployDialog();
    else if (preset.custom) addProvider();
    else _openLocalEndpointDialog(preset);
  };
  return item;
}

// ─────────────────────────── shared chat handoff ───────────────────────────

function _ldStartChat(promptText) {
  closeSettings();
  // newChat() reads the current draft to archive the previous conversation,
  // so the prompt must land in the input only after the new shell exists.
  newChat();
  var input = document.getElementById('userInput');
  if (!input) return;
  input.value = promptText;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  updateSendButton();
  input.focus();
}

function _ldCopy(text, btn) {
  function done() {
    var prev = btn.textContent;
    btn.textContent = t('settings.localRecCopied');
    setTimeout(function() { btn.textContent = prev; }, 1500);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, function() {});
  }
}

// ─────────────────────────── hardware card ───────────────────────────

function _ldGib(n) {
  return (typeof n === 'number' && isFinite(n)) ? (n / 1073741824).toFixed(1) : '?';
}

function _ldHardwareHtml(hw) {
  var parts = [];
  var gpus = (hw && hw.gpus) || [];
  if (gpus.length) {
    gpus.forEach(function(g) {
      parts.push('<span class="stg-ld-hw-item">GPU' + escapeHtml(String(g.index)) + ' ' +
        escapeHtml(String(g.name || '?')) + ' · ' +
        escapeHtml(t('settings.localHwVram', {
          free: _ldGib(g.vram_free_bytes), total: _ldGib(g.vram_total_bytes),
        })) + '</span>');
    });
  } else {
    parts.push('<span class="stg-ld-hw-item stg-ld-hw-warn">' +
      escapeHtml(t('settings.localHwNoGpu')) + '</span>');
  }
  parts.push('<span class="stg-ld-hw-item">' +
    escapeHtml(t('settings.localHwRam', {
      free: _ldGib(hw && hw.ram_available_bytes),
      total: _ldGib(hw && hw.ram_total_bytes),
    })) + '</span>');
  parts.push('<span class="stg-ld-hw-item">' +
    escapeHtml(t('settings.localHwDisk', {
      free: _ldGib(hw && hw.disk_free_bytes),
    })) + '</span>');
  return parts.join('');
}

function _ldLoadHardware(modal) {
  var box = modal.querySelector('[data-ld="hardware"]');
  if (!box) return;
  box.innerHTML = '<span class="stg-ld-hw-item">' +
    escapeHtml(t('settings.localRecLoading')) + '</span>';
  Promise.resolve(Api.localServe.hardware()).then(function(data) {
    if (!box.isConnected) return;
    if (!data || !data.hardware) {
      box.innerHTML = '<span class="stg-ld-hw-item stg-ld-hw-warn">' +
        escapeHtml(t('settings.localRecError')) + '</span>';
      return;
    }
    box.innerHTML = _ldHardwareHtml(data.hardware);
  });
}

// ─────────────────────────── recommendations ───────────────────────────

function _ldRecBrand(cand) {
  var id = String((cand && cand.id) || '');
  if (id.indexOf('qwen') === 0) return 'qwen';
  if (id.indexOf('deepseek') === 0) return 'deepseek';
  return 'local';
}

function _ldRecCard(cand) {
  var best = cand.best || {};
  var download = best.download || {};
  var tier = String(cand.tier || 'comfortable');
  var card = document.createElement('div');
  card.className = 'stg-ld-rec';
  card.innerHTML =
    '<span class="stg-ld-rec-icon">' + _brandSvg(_ldRecBrand(cand), 22) + '</span>' +
    '<span class="stg-ld-rec-main">' +
      '<span class="stg-ld-rec-title"><strong>' + escapeHtml(String(cand.display || '')) +
        '</strong><span class="stg-ld-fit ' + (_LD_TIER_CLASS[tier] || '') + '">' +
        escapeHtml(t(_LD_TIER_LABEL_KEY[tier] || _LD_TIER_LABEL_KEY.comfortable)) +
      '</span></span>' +
      '<span class="stg-ld-rec-sub">' +
        escapeHtml(t('settings.localRecSub', {
          params: String(cand.params_b),
          variant: String(best.variant || ''),
          engine: String(best.engine || ''),
          size: _ldGib(best.weight_bytes),
        })) + '</span>' +
      (cand.note ? '<span class="stg-ld-rec-note">' +
        escapeHtml(String(cand.note)) + '</span>' : '') +
      (download.hf_cmd ? '<span class="stg-ld-rec-cmd"><code>' +
        escapeHtml(String(download.hf_cmd)) +
        '</code><button type="button" class="stg-ld-copy">' +
        escapeHtml(t('settings.localRecCopyCmd')) + '</button></span>' : '') +
    '</span>' +
    '<button type="button" class="stg-btn-add stg-ld-rec-go">' +
      escapeHtml(t('settings.localRecDeploy')) + '</button>';
  var copyBtn = card.querySelector('.stg-ld-copy');
  if (copyBtn) {
    copyBtn.onclick = function() { _ldCopy(String(download.hf_cmd || ''), copyBtn); };
  }
  card.querySelector('.stg-ld-rec-go').onclick = function() {
    _ldStartChat(t('settings.localRecDeployPrompt', {
      model: String(cand.display || ''),
      variant: String(best.variant || ''),
      engine: String(best.engine || ''),
      cmd: String(download.hf_cmd || ''),
    }));
  };
  return card;
}

function _ldCloudCard(alt) {
  var pricing = (alt && alt.pricing) || {};
  var card = document.createElement('div');
  card.className = 'stg-ld-cloud';
  card.innerHTML =
    '<span class="stg-ld-rec-icon">' + _brandSvg('deepseek', 22) + '</span>' +
    '<span class="stg-ld-rec-main">' +
      '<span class="stg-ld-rec-title"><strong>' +
        escapeHtml(t('settings.localCloudTitle', { name: String(alt.display || '') })) +
      '</strong></span>' +
      '<span class="stg-ld-rec-note">' + escapeHtml(String(alt.reason || '')) + '</span>' +
      '<span class="stg-ld-rec-sub">' +
        escapeHtml(t('settings.localCloudPrice', {
          input: String(pricing.input), output: String(pricing.output),
        })) + '</span>' +
    '</span>' +
    '<button type="button" class="stg-btn-add stg-ld-cloud-go">' +
      escapeHtml(t('settings.localCloudAdd')) + '</button>';
  card.querySelector('.stg-ld-cloud-go').onclick = function() {
    var overlay = document.getElementById('stgLocalDeployModal');
    if (overlay) overlay.remove();
    void _addTemplateProvider(String(alt.template_key || 'deepseek'));
  };
  return card;
}

function _ldLoadRecommendations(modal, purpose) {
  var box = modal.querySelector('[data-ld="recs"]');
  if (!box) return;
  box.innerHTML = '<div class="stg-ld-hint">' +
    escapeHtml(t('settings.localRecLoading')) + '</div>';
  Promise.resolve(Api.localServe.recommendations(purpose)).then(function(data) {
    if (!box.isConnected) return;
    box.innerHTML = '';
    if (!data) {
      box.innerHTML = '<div class="stg-ld-hint">' +
        escapeHtml(t('settings.localRecError')) + '</div>';
      return;
    }
    var candidates = data.candidates || [];
    if (!candidates.length) {
      box.innerHTML = '<div class="stg-ld-hint">' +
        escapeHtml(t('settings.localRecEmpty')) + '</div>';
    } else {
      candidates.forEach(function(cand) { box.appendChild(_ldRecCard(cand)); });
    }
    if (data.cloud_alternative) box.appendChild(_ldCloudCard(data.cloud_alternative));
  });
}

function _ldPurposeBar(modal) {
  var bar = document.createElement('div');
  bar.className = 'stg-ld-purposes';
  _LD_PURPOSES.forEach(function(purpose) {
    var chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'stg-ld-purpose' + (purpose === '' ? ' is-active' : '');
    chip.textContent = t('settings.localRecPurpose' +
      (purpose ? purpose.charAt(0).toUpperCase() + purpose.slice(1) : 'All'));
    chip.onclick = function() {
      bar.querySelectorAll('.stg-ld-purpose').forEach(function(row) {
        row.classList.toggle('is-active', row === chip);
      });
      _ldLoadRecommendations(modal, purpose);
    };
    bar.appendChild(chip);
  });
  return bar;
}

// ─────────────────────────── managed instances ───────────────────────────

function _ldInstanceRow(inst) {
  var status = String(inst.status || '');
  var labelKey = _LD_STATUS_LABEL_KEY[status];
  var row = document.createElement('div');
  row.className = 'stg-ld-inst';
  var diagnosis = inst.diagnosis || null;
  row.innerHTML =
    '<span class="stg-ld-inst-main">' +
      '<span class="stg-ld-rec-title"><strong>' +
        escapeHtml(String(inst.served_name || inst.id || '')) +
        '</strong><span class="stg-ld-state is-' + escapeHtml(status) + '">' +
        escapeHtml(labelKey ? t(labelKey) : status) + '</span></span>' +
      '<span class="stg-ld-rec-sub">' +
        escapeHtml(String(inst.engine || '')) +
        (inst.base_url ? ' · ' + escapeHtml(String(inst.base_url)) : '') +
      '</span>' +
      (diagnosis ? '<span class="stg-ld-rec-note">' +
        escapeHtml(String(diagnosis.summary || '')) + '</span>' : '') +
    '</span>';
  if (status === 'failed') {
    var repair = document.createElement('button');
    repair.type = 'button';
    repair.className = 'stg-btn-add stg-ld-inst-fix';
    repair.textContent = t('settings.localManagedRepair');
    repair.onclick = function() {
      _ldStartChat(t('settings.localRepairPrompt', {
        id: String(inst.id || ''),
        model: String(inst.served_name || inst.model_path || ''),
        engine: String(inst.engine || ''),
      }));
    };
    row.appendChild(repair);
  } else if (status === 'stopped' || status === 'planned') {
    var start = document.createElement('button');
    start.type = 'button';
    start.className = 'stg-btn-add stg-ld-inst-start';
    start.textContent = t('settings.localManagedStart');
    start.onclick = function() {
      _ldStartChat(t('settings.localStartPrompt', {
        id: String(inst.id || ''),
        model: String(inst.served_name || inst.model_path || ''),
        engine: String(inst.engine || ''),
      }));
    };
    row.appendChild(start);
  }
  return row;
}

function _ldLoadDeployments(modal) {
  var box = modal.querySelector('[data-ld="instances"]');
  if (!box) return;
  Promise.resolve(Api.localServe.deployments()).then(function(data) {
    if (!box.isConnected) return;
    box.innerHTML = '';
    var rows = (data && data.instances) || [];
    if (!rows.length) {
      box.innerHTML = '<div class="stg-ld-hint">' +
        escapeHtml(t('settings.localManagedEmpty')) + '</div>';
      return;
    }
    rows.forEach(function(inst) { box.appendChild(_ldInstanceRow(inst)); });
  });
}

// ─────────────────────────── the page ───────────────────────────

function addLocalProvider() {
  var prev = document.getElementById('stgLocalDeployModal');
  if (prev) prev.remove();
  var overlay = document.createElement('div');
  overlay.id = 'stgLocalDeployModal';
  overlay.className = 'stg-modal-overlay';
  var modal = document.createElement('div');
  modal.className = 'stg-modal stg-tpl-wizard stg-local-deploy';
  modal.innerHTML = '<div class="stg-modal-header"><span class="stg-modal-title">' +
    escapeHtml(t('settings.localPresetTitle')) +
    '</span><button type="button" class="stg-modal-close">✕</button></div>' +
    '<div class="stg-modal-body"><p class="stg-modal-desc">' +
    escapeHtml(t('settings.localPresetDesc')) + '</p>' +
    '<div class="stg-local-group-label">' +
    escapeHtml(t('settings.localRecGroupTitle')) + '</div>' +
    '<div class="stg-ld-hw" data-ld="hardware"></div>' +
    '<div data-ld="purposes"></div>' +
    '<div class="stg-ld-recs" data-ld="recs"></div>' +
    '<div class="stg-local-group-label">' +
    escapeHtml(t('settings.localManagedGroup')) + '</div>' +
    '<div class="stg-ld-instances" data-ld="instances"></div>' +
    '<div class="stg-local-group-label">' +
    escapeHtml(t('settings.localPresetGroupEngines')) + '</div>' +
    '<div class="stg-template-grid" data-group="engines"></div>' +
    '<div class="stg-local-group-label">' +
    escapeHtml(t('settings.localPresetGroupOther')) + '</div>' +
    '<div class="stg-template-grid" data-group="other"></div></div>';
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  modal.querySelector('[data-ld="purposes"]').appendChild(_ldPurposeBar(modal));
  var enginesGrid = modal.querySelector('[data-group="engines"]');
  var otherGrid = modal.querySelector('[data-group="other"]');
  _LOCAL_DEPLOY_PRESETS.forEach(function(preset) {
    var tile = _localDeployPresetTile(preset, overlay);
    // Managed + custom stay below the four inference engines (owner-ratified
    // order 2026-07-25); grouping is purely visual.
    if (preset.engine === 'managed' || preset.custom) otherGrid.appendChild(tile);
    else enginesGrid.appendChild(tile);
  });
  _localDeployWireClose(overlay, modal);
  _ldLoadHardware(modal);
  _ldLoadRecommendations(modal, '');
  _ldLoadDeployments(modal);
}

function _openLocalEndpointDialog(preset) {
  var prev = document.getElementById('stgLocalEndpointModal');
  if (prev) prev.remove();
  var overlay = document.createElement('div');
  overlay.id = 'stgLocalEndpointModal';
  overlay.className = 'stg-modal-overlay';
  var modal = document.createElement('div');
  modal.className = 'stg-modal stg-tpl-wizard';
  modal.innerHTML = '<div class="stg-modal-header"><span class="stg-modal-title">' +
    _brandSvg(preset.icon, 18) + ' ' + escapeHtml(preset.name) +
    '</span><button type="button" class="stg-modal-close">✕</button></div>' +
    '<div class="stg-modal-body"><p class="stg-modal-desc">' +
    escapeHtml(t(preset.descKey)) + '</p>' +
    '<label class="stg-tpl-wizard-keylabel">' +
    escapeHtml(t('settings.localDeployEndpointsLabel')) +
    '<textarea class="stg-local-endpoints-input" rows="4" placeholder="' +
    escapeHtml(preset.placeholder) + '"></textarea></label>' +
    '<label class="stg-tpl-wizard-keylabel">' +
    escapeHtml(t('settings.localDeployApiKeyLabel')) +
    '<input type="password" class="stg-tpl-wizard-key" autocomplete="new-password"></label>' +
    '<div class="stg-auto-status" style="display:none"></div>' +
    '<div class="stg-local-endpoint-results"></div></div>' +
    '<div class="stg-modal-footer"><button type="button" class="stg-btn-secondary">' +
    escapeHtml(t('settings.epBulkCancel')) + '</button>' +
    '<button type="button" class="stg-btn-add">' +
    escapeHtml(t('settings.localDeployProbeAdd')) + '</button></div>';
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  var close = _localDeployWireClose(overlay, modal);
  modal.querySelector('.stg-btn-secondary').onclick = close;
  var textarea = modal.querySelector('.stg-local-endpoints-input');
  var keyInput = modal.querySelector('.stg-tpl-wizard-key');
  var statusEl = modal.querySelector('.stg-auto-status');
  var resultsEl = modal.querySelector('.stg-local-endpoint-results');
  var probeBtn = modal.querySelector('.stg-btn-add');
  function setStatus(text, kind) {
    statusEl.style.display = text ? '' : 'none';
    statusEl.className = 'stg-auto-status' + (kind ? ' ' + kind : '');
    statusEl.textContent = text;
  }
  probeBtn.onclick = async function() {
    var urls = [];
    textarea.value.split('\n').forEach(function(line) {
      var url = line.trim();
      if (url && urls.indexOf(url) === -1) urls.push(url);
    });
    if (!urls.length) {
      showAlert(t('settings.localDeployNoUrl'));
      return;
    }
    var apiKey = keyInput.value.trim();
    probeBtn.disabled = true;
    resultsEl.innerHTML = '';
    setStatus(t('settings.epProbingN', { n: urls.length }), 'stg-auto-loading');
    var probes = await Promise.allSettled(urls.map(function(url) {
      return Api.providers.probe(url, apiKey, '');
    }));
    var okCount = 0;
    for (var i = 0; i < urls.length; i++) {
      var row = document.createElement('div');
      row.className = 'stg-local-endpoint-row';
      var probed = probes[i];
      var bundle = probed.status === 'fulfilled' &&
        probed.value && probed.value.provider_bundle;
      if (bundle) {
        try {
          // Sequential staging: each success bumps the v2 revision.
          if (await _stageModelRoutingProviderBundle(bundle, apiKey)) {
            okCount++;
            row.classList.add('is-ok');
            row.textContent = urls[i] + ' · ' + t('settings.epModelsCount', {
              n: (bundle.deployments || []).length,
            });
          } else {
            row.classList.add('is-fail');
            row.textContent = urls[i] + ' · ' + t('settings.localDeployDuplicate');
          }
        } catch (error) {
          row.classList.add('is-fail');
          row.textContent = urls[i] + ' · ' + String(error && error.message || error);
        }
      } else {
        var reason = probed.status === 'rejected'
          ? String(probed.reason && probed.reason.message || probed.reason)
          : String((probed.value && (probed.value.error || probed.value.message)) ||
              t('settings.epProbeFailed'));
        row.classList.add('is-fail');
        row.textContent = urls[i] + ' · ' + reason;
      }
      resultsEl.appendChild(row);
    }
    if (okCount) {
      setStatus(t('settings.localDeployAddedSummary', {
        ok: okCount, total: urls.length,
      }), 'stg-auto-success');
    } else {
      setStatus(t('settings.localDeployNoneOk'), 'stg-auto-error');
    }
    probeBtn.disabled = false;
  };
  textarea.focus();
}

function _openManagedDeployDialog() {
  var prev = document.getElementById('stgManagedDeployModal');
  if (prev) prev.remove();
  var overlay = document.createElement('div');
  overlay.id = 'stgManagedDeployModal';
  overlay.className = 'stg-modal-overlay';
  var modal = document.createElement('div');
  modal.className = 'stg-modal stg-tpl-wizard';
  var engineOptions = [
    { value: '', label: t('settings.managedDeployEngineAuto') },
    { value: 'vllm', label: 'vLLM' },
    { value: 'sglang', label: 'SGLang' },
    { value: 'ollama', label: 'Ollama' },
    { value: 'llamacpp', label: 'llama.cpp' },
  ];
  modal.innerHTML = '<div class="stg-modal-header"><span class="stg-modal-title">' +
    _brandSvg('local', 18) + ' ' + escapeHtml(t('settings.managedDeployTitle')) +
    '</span><button type="button" class="stg-modal-close">✕</button></div>' +
    '<div class="stg-modal-body"><p class="stg-modal-desc">' +
    escapeHtml(t('settings.managedDeployDesc')) + '</p>' +
    '<label class="stg-tpl-wizard-keylabel">' +
    escapeHtml(t('settings.managedDeployPathLabel')) +
    '<input type="text" class="stg-tpl-wizard-key stg-managed-path" placeholder="' +
    escapeHtml(t('settings.managedDeployPathHint')) + '"></label>' +
    '<label class="stg-tpl-wizard-keylabel">' +
    escapeHtml(t('settings.managedDeployEngineLabel')) +
    '<select class="stg-tpl-wizard-key stg-managed-engine">' +
    engineOptions.map(function(option) {
      return '<option value="' + option.value + '">' + escapeHtml(option.label) + '</option>';
    }).join('') + '</select></label></div>' +
    '<div class="stg-modal-footer"><button type="button" class="stg-btn-secondary">' +
    escapeHtml(t('settings.epBulkCancel')) + '</button>' +
    '<button type="button" class="stg-btn-add">' +
    escapeHtml(t('settings.managedDeployStart')) + '</button></div>';
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  var close = _localDeployWireClose(overlay, modal);
  modal.querySelector('.stg-btn-secondary').onclick = close;
  var pathInput = modal.querySelector('.stg-managed-path');
  var engineSelect = modal.querySelector('.stg-managed-engine');
  modal.querySelector('.stg-btn-add').onclick = function() {
    var path = pathInput.value.trim();
    if (!path) {
      showAlert(t('settings.managedDeployPathRequired'));
      return;
    }
    var engineLabel = engineSelect.options[engineSelect.selectedIndex].text;
    close();
    _startManagedDeployChat(path, engineLabel);
  };
  pathInput.focus();
}

function _startManagedDeployChat(path, engineLabel) {
  closeSettings();
  // newChat() reads the current draft to archive the previous conversation,
  // so the prompt must land in the input only after the new shell exists.
  newChat();
  var input = document.getElementById('userInput');
  if (!input) return;
  input.value = t('settings.managedDeployPrompt', { path: path, engine: engineLabel });
  input.dispatchEvent(new Event('input', { bubbles: true }));
  updateSendButton();
  input.focus();
}
