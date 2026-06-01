// ══════════════════════════════════════════════════════════════════
//  api-keys.js — API key management UI (Settings → API Keys tab)
//
//  All business logic lives on the server (lib/api_keys.py +
//  routes/api_v1/keys.py). This file is a pure fetch + render layer
//  per CLAUDE.md §16 (Frontend/Backend Boundary).
//
//  Public functions (used by index.html):
//    apiKeysOpen()        — called when the API Keys tab is activated
//    apiKeysCreate()      — POST /api/v1/keys
//    apiKeysRevoke(id)    — DELETE /api/v1/keys/<id>
//    apiKeysToggle(id, disabled) — PATCH /api/v1/keys/<id>
//    apiKeysShowUsage(id) — GET  /api/v1/usage?key_id=<id>
//    apiKeysCopyToken()   — copy the just-created token to clipboard
//    apiKeysDismissReveal() — hide the plaintext token panel
// ══════════════════════════════════════════════════════════════════

(function() {
  'use strict';

  // The closed scope vocabulary is loaded from /api/v1/capabilities so
  // the UI never falls out of sync with the server enum.
  let _allScopes = [];
  let _lastCreatedToken = '';

  function _esc(s) {
    if (typeof escapeHtml === 'function') return escapeHtml(s);
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function _fmtDate(ts) {
    if (!ts) return '—';
    try {
      const d = new Date(ts * 1000);
      return d.toLocaleString();
    } catch (_) { return '—'; }
  }

  function _fmtNum(n) {
    if (n == null) return '—';
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
    return String(n);
  }

  async function _fetchJson(url, opts) {
    const r = await fetch(typeof apiUrl === 'function' ? apiUrl(url) : url,
      Object.assign({ credentials: 'same-origin' }, opts || {}));
    let body = null;
    try { body = await r.json(); } catch (_) { body = null; }
    if (!r.ok) {
      const msg = (body && (body.error?.detail || body.error || body.detail))
        || ('HTTP ' + r.status);
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return body || {};
  }

  async function apiKeysOpen() {
    await _refreshAuthMode();
    await _loadScopes();
    await _refreshList();
  }
  window.apiKeysOpen = apiKeysOpen;

  // ── Auth-mode card ─────────────────────────────────────────────
  // Renders the open / private / multi-user radio + explanation
  // inside #authModeBlock. The current state comes from
  // /api/v1/auth/mode (public read), and PUT goes through the same
  // endpoint (admin-scoped on the server).
  async function _refreshAuthMode() {
    const block = document.getElementById('authModeBlock');
    if (!block) return;
    let state;
    try {
      state = await _fetchJson('/api/v1/auth/mode');
    } catch (e) {
      block.innerHTML = '<span style="color:var(--accent-danger,#e25)">'
        + '加载失败：' + _esc(e.message) + '</span>';
      return;
    }
    const cur = state.mode || 'open';
    const envLocked = !!state.env_locked;
    const modes = state.modes || ['open', 'private', 'multi-user'];
    const labels = {
      'open':       { name: '开放',        hint: '不需要令牌；适合本机个人使用，浏览器直接打开即可。',         icon: '🔓' },
      'private':    { name: '私有',        hint: '所有非公开路由必须携带 Bearer 令牌或 cookie。',           icon: '🔒' },
      'multi-user': { name: '多用户',      hint: '同私有模式，但语义上将本实例作为他人调用 Tofu 的中继站。', icon: '👥' },
    };
    const radios = modes.map(m => {
      const meta = labels[m] || { name: m, hint: '', icon: '' };
      return `
        <label style="display:block; margin:6px 0; cursor:${envLocked ? 'not-allowed' : 'pointer'};
                      ${envLocked ? 'opacity:0.6;' : ''}">
          <input type="radio" name="authMode" value="${_esc(m)}"
                 ${m === cur ? 'checked' : ''}
                 ${envLocked ? 'disabled' : ''}
                 onchange="apiKeysSetMode(this.value)">
          <strong>${meta.icon} ${_esc(meta.name)}</strong>
          <span style="opacity:0.65; margin-left:6px; font-size:12px;">${_esc(meta.hint)}</span>
        </label>`;
    }).join('');
    const sourceNote = state.source === 'env'
      ? '<div style="margin-top:8px; padding:8px; border-left:3px solid var(--accent-warn,#e0a800); background:rgba(224,168,0,0.06); font-size:12px;">'
        + 'TOFU_AUTH_MODE 环境变量已锁定模式。在不重启的情况下无法在 UI 中更改。'
        + '</div>'
      : '';
    const openWarning = (cur === 'open' && !envLocked)
      ? '<div style="margin-top:8px; padding:8px; border-left:3px solid var(--accent,#5a8); background:rgba(80,180,160,0.06); font-size:12px;">'
        + '当前为开放模式：下方 API 密钥对鉴权不起作用，仅作为以后切换到私有/多用户模式时使用。'
        + '</div>'
      : '';
    block.innerHTML = radios + sourceNote + openWarning;
  }

  async function apiKeysSetMode(newMode) {
    if (!newMode) return;
    if (newMode === 'open' &&
        !confirm('切换到开放模式将禁用所有令牌鉴权，任何能访问本服务的人都可以调用所有 API。确认继续？')) {
      await _refreshAuthMode();  // restore radio
      return;
    }
    try {
      await _fetchJson('/api/v1/auth/mode', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: newMode }),
      });
    } catch (e) {
      alert('切换失败：' + e.message);
    }
    await _refreshAuthMode();
    await _refreshList();
  }
  window.apiKeysSetMode = apiKeysSetMode;

  async function _loadScopes() {
    if (_allScopes.length) {
      _renderScopeCheckboxes();
      return;
    }
    try {
      const caps = await _fetchJson('/api/v1/capabilities');
      _allScopes = (caps.scopes || []).filter(s => s !== 'admin');
      _renderScopeCheckboxes();
    } catch (e) {
      console.warn('[ApiKeys] capabilities fetch failed:', e.message);
      // Hard-coded fallback so the UI is still usable.
      _allScopes = [
        'chat', 'tasks', 'conversations', 'files',
        'agents:paper', 'agents:translate', 'agents:swarm',
        'agents:scheduler', 'agents:memory', 'agents:browser',
        'agents:trading', 'agents:image', 'agents:mcp',
        'webhooks', 'capabilities', 'usage',
      ];
      _renderScopeCheckboxes();
    }
  }

  function _renderScopeCheckboxes() {
    const root = document.getElementById('apiKeyScopesList');
    if (!root) return;
    const defaults = new Set(['chat', 'tasks']);
    const html = _allScopes.map(s => `
      <label style="display:inline-flex; align-items:center; gap:6px;
                    margin-right:10px; margin-bottom:4px; font-size:12px;">
        <input type="checkbox" data-scope="${_esc(s)}"
               ${defaults.has(s) ? 'checked' : ''}>
        <code style="font-size:11px;">${_esc(s)}</code>
      </label>`).join('');
    root.innerHTML = html + `
      <label style="display:inline-flex; align-items:center; gap:6px;
                    margin-left:12px; padding-left:12px;
                    border-left:1px solid var(--border); font-size:12px;">
        <input type="checkbox" id="apiKeyAdminScope">
        <strong style="color:var(--accent-warn,#e0a800)">admin</strong>
      </label>`;
  }

  function _selectedScopes() {
    const out = [];
    document.querySelectorAll('#apiKeyScopesList input[type=checkbox][data-scope]')
      .forEach(cb => { if (cb.checked) out.push(cb.dataset.scope); });
    return out;
  }

  async function apiKeysCreate() {
    const name = (document.getElementById('apiKeyName').value || '').trim();
    if (!name) { alert('请输入名称'); return; }
    const rpm = parseInt(document.getElementById('apiKeyRpm').value, 10) || 0;
    const tpd = parseInt(document.getElementById('apiKeyTpd').value, 10) || 0;
    const scopes = _selectedScopes();
    const adminBox = document.getElementById('apiKeyAdminScope');
    const admin = !!(adminBox && adminBox.checked);
    if (!scopes.length && !admin) {
      alert('请至少选择一个范围');
      return;
    }
    try {
      const body = await _fetchJson('/api/v1/keys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name, scopes, rate_limit_rpm: rpm, rate_limit_tpd: tpd, admin,
        }),
      });
      _lastCreatedToken = body.token || '';
      const code = document.getElementById('apiKeyPlaintext');
      const panel = document.getElementById('apiKeyJustCreated');
      if (code) code.textContent = _lastCreatedToken;
      if (panel) panel.style.display = 'block';
      // Reset form fields.
      document.getElementById('apiKeyName').value = '';
      await _refreshList();
    } catch (e) {
      alert('创建失败：' + e.message);
    }
  }
  window.apiKeysCreate = apiKeysCreate;

  function apiKeysCopyToken() {
    if (!_lastCreatedToken) return;
    navigator.clipboard.writeText(_lastCreatedToken).then(() => {
      const btn = document.querySelector('#apiKeyJustCreated button');
      if (btn) {
        const orig = btn.textContent;
        btn.textContent = '已复制 ✓';
        setTimeout(() => { btn.textContent = orig; }, 1500);
      }
    });
  }
  window.apiKeysCopyToken = apiKeysCopyToken;

  function apiKeysDismissReveal() {
    _lastCreatedToken = '';
    const panel = document.getElementById('apiKeyJustCreated');
    if (panel) panel.style.display = 'none';
    const code = document.getElementById('apiKeyPlaintext');
    if (code) code.textContent = '';
  }
  window.apiKeysDismissReveal = apiKeysDismissReveal;

  async function _refreshList() {
    const root = document.getElementById('apiKeysList');
    if (!root) return;
    root.innerHTML = '<em style="opacity:0.5">正在加载…</em>';
    try {
      const body = await _fetchJson('/api/v1/keys');
      const keys = body.keys || [];
      if (!keys.length) {
        root.innerHTML = '<em style="opacity:0.5">尚未颁发任何密钥</em>';
        return;
      }
      root.innerHTML = `
        <table class="settings-table">
          <thead>
            <tr>
              <th>名称</th><th>前缀</th><th>范围</th>
              <th>RPM</th><th>每日 Tok</th>
              <th>创建时间</th><th>最后使用</th><th>状态</th><th></th>
            </tr>
          </thead>
          <tbody>${keys.map(_renderRow).join('')}</tbody>
        </table>`;
    } catch (e) {
      root.innerHTML = '<span style="color:var(--accent-danger,#e25)">加载失败：' +
        _esc(e.message) + '</span>';
    }
  }

  function _renderRow(k) {
    const scopes = (k.scopes || []).map(s =>
      `<code style="font-size:10px;background:var(--bg-secondary);padding:1px 4px;border-radius:3px;margin:1px;">${_esc(s)}</code>`
    ).join(' ');
    const status = k.disabled
      ? '<span style="color:var(--accent-danger,#e25)">已停用</span>'
      : '<span style="color:var(--accent-ok,#3a3)">启用</span>';
    return `
      <tr data-key-id="${_esc(k.id)}">
        <td><strong>${_esc(k.name)}</strong></td>
        <td><code style="font-size:11px;">${_esc(k.prefix)}…</code></td>
        <td style="max-width:200px">${scopes}</td>
        <td>${k.rate_limit_rpm || '—'}</td>
        <td>${_fmtNum(k.rate_limit_tpd)}</td>
        <td style="font-size:11px;opacity:0.7">${_fmtDate(k.created_at)}</td>
        <td style="font-size:11px;opacity:0.7">${_fmtDate(k.last_used_at)}</td>
        <td>${status}</td>
        <td style="white-space:nowrap;">
          <button class="btn btn-secondary btn-xs"
                  onclick="apiKeysShowUsage('${_esc(k.id)}')"
                  data-i18n="settings.apiKeyShowUsage">用量</button>
          <button class="btn btn-secondary btn-xs"
                  onclick="apiKeysToggle('${_esc(k.id)}', ${!k.disabled})">
            ${k.disabled ? '启用' : '停用'}
          </button>
          <button class="btn btn-danger btn-xs"
                  onclick="apiKeysRevoke('${_esc(k.id)}', '${_esc(k.name)}')"
                  data-i18n="settings.apiKeyRevoke">撤销</button>
        </td>
      </tr>`;
  }

  async function apiKeysToggle(keyId, newDisabled) {
    try {
      await _fetchJson('/api/v1/keys/' + encodeURIComponent(keyId), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ disabled: newDisabled }),
      });
      await _refreshList();
    } catch (e) {
      alert('操作失败：' + e.message);
    }
  }
  window.apiKeysToggle = apiKeysToggle;

  async function apiKeysRevoke(keyId, name) {
    if (!confirm('撤销密钥「' + name + '」？此操作无法撤销。')) return;
    try {
      await _fetchJson('/api/v1/keys/' + encodeURIComponent(keyId),
        { method: 'DELETE' });
      await _refreshList();
    } catch (e) {
      alert('撤销失败：' + e.message);
    }
  }
  window.apiKeysRevoke = apiKeysRevoke;

  async function apiKeysShowUsage(keyId) {
    const block = document.getElementById('apiKeyUsageBlock');
    if (!block) return;
    block.innerHTML = '<em style="opacity:0.5">正在加载…</em>';
    try {
      const body = await _fetchJson(
        '/api/v1/usage?days=30&key_id=' + encodeURIComponent(keyId));
      const days = body.days || [];
      const max = days.reduce((m, d) => Math.max(m, d.requests || 0), 1);
      const bars = days.map(d => {
        const h = Math.max(2, Math.round((d.requests / max) * 60));
        return `<div title="${_esc(d.date)}: ${d.requests} 请求 · ${d.tokens} tok"
                     style="display:inline-block; width:8px; margin:0 1px;
                            background:var(--accent); height:${h}px;
                            vertical-align:bottom; border-radius:2px;"></div>`;
      }).join('');
      block.innerHTML = `
        <div style="margin-bottom:8px;">
          <strong>${_esc(keyId)}</strong> ·
          总请求 <code>${_fmtNum(body.total.requests)}</code> ·
          总 Token <code>${_fmtNum(body.total.tokens)}</code>
        </div>
        <div style="border:1px solid var(--border); padding:8px; border-radius:6px;
                    overflow-x:auto; white-space:nowrap;">
          ${bars}
        </div>`;
    } catch (e) {
      block.innerHTML = '<span style="color:var(--accent-danger,#e25)">加载失败：' +
        _esc(e.message) + '</span>';
    }
  }
  window.apiKeysShowUsage = apiKeysShowUsage;

  // Auto-load when the API Keys tab is activated. Hook into the
  // existing switchSettingsTab() function (a small monkey-patch is the
  // cleanest path that doesn't require modifying settings.js).
  const _origSwitch = window.switchSettingsTab;
  if (typeof _origSwitch === 'function') {
    window.switchSettingsTab = function(tabId) {
      _origSwitch.call(this, tabId);
      if (tabId === 'apikeys') {
        apiKeysOpen().catch(e =>
          console.warn('[ApiKeys] open failed:', e));
      }
    };
  }
})();
