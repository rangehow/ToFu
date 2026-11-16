/* ===== migrated source: local-control.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   local-control — the SINGLE "let Tofu act on my machine" surface.

   Merges what used to be two toolbar rows (#browserToggle + #desktopToggle),
   one setup modal (#browserModal) and one blind desktop flag flip (with
   no status check at all). From the user's side browser-tabs and
   computer-control are one concept; two rows, two modals and two status dots
   were strictly more cognitive load than one.

   What did NOT merge, deliberately: the two backing flags `browserEnabled`
   and `desktopEnabled` stay separate on the wire. They gate different tool
   families with genuinely different risk tiers (reading a tab vs running a
   shell command), and `lib/tools/registry/_build.py` builds them from two
   independent ToolContext fields. Only the SURFACE is merged. The browser
   flag has no switch: it is always on (its tools need a connected extension
   and stay Tool-Search deferred), so only the desktop row carries one.

   ── The one rule this file exists to enforce ──
   Each capability row shows exactly ONE next action, chosen by DETECTED
   state. Never a menu of every possible path, and never an instruction the
   user cannot act on from where they are. The desktop choice is made by the
   BACKEND (`setup_state` on /api/v4/desktop/status): `connected` when one of
   the caller's nodes polls, else `pair`. Every Tofu desktop install is a
   node that dials OUT to the servers it joined, so the one next action is
   always the same: mint a pairing link here, paste it into the desktop app
   (contracts/desktop_node_v1.schema.json).

   ── …and the FLOOR that rule stands on ──
   "Chosen by detected state" used to mean "rendered only after detection":
   the modal opened showing `local.checking` ("正在检查…") over an EMPTY setup
   box, and the install instructions appeared one or two network round-trips
   later. Every user paid a wait to be told the thing that is true for almost
   all of them — and the failure modes were worse than the wait: if the status
   call errored the box was blanked back to empty, and if `Api` was not yet
   defined `_lcRefresh` returned without painting anything at all, leaving
   "正在检查…" and an empty box on screen permanently.

   So detection now UPGRADES an instruction that is already on screen rather
   than being the thing that puts one there. `_lcPaintFloor` runs
   synchronously on open with the guidance that holds regardless of what the
   probe finds (download the extension / install the desktop app), and the
   renderers replace it with something MORE specific once the payload lands.
   The floor is never "loading" and never empty, so the worst case is an
   instruction that is merely generic — never one that is absent.

   Corollary, and the reason the download markup lives in `_lcBrowserDownload`
   rather than inline: the floor and the detected `download` state are the
   SAME instruction, so they must be ONE authoring. Two copies of it would
   drift, and a drifted floor is a wrong instruction shown first.

   This demand-loaded owner contains the modal, probes, downloads and browser
   relay. The retained local-control-state.js owns only the toolbar badge, so
   ordinary coding and writing sessions do not parse this workbench.
   ═══════════════════════════════════════════════════════════════════ */

/* Poll cadence while the modal is OPEN. `is_desktop_agent_connected()` is a
 * 15s window (lib/desktop/bridge.py::_CONNECTED_WINDOW_S) and enabling the
 * tray agent takes a couple of seconds, so a user who turns it on WHILE
 * looking at this dialog must see the dot flip without reopening it. The old
 * _checkBrowserStatus was one-shot-on-open; that limitation is not carried
 * over. Cleared on close so a background tab never polls. */
var _LC_POLL_MS = 3000;
var _lcPollTimer = null;

/* Last CONFIRMED reachability per capability, written only from a real status
 * response (the renderers, or _lcProbeReach for the toolbar row). `null` =
 * never confirmed, which is NOT the same
 * as "unreachable" — an unchecked capability must never be presented as
 * broken. Read by the switch repaint (which would otherwise have to guess) and
 * by the badge. */
var _lcReach = { browser: null, desktop: null };
var LocalControlPresentationState = Object.freeze({
  reach: _lcReach,
  refresh: _lcProbeReach,
});

/* Signatures and explicit entry intent used by the setup renderers.
 * `_lcDesktopSigLast = null` forces a desktop render on reopen. Settings can
 * also open this modal because a site adapter negotiated a concrete browser
 * capability gap even when a legacy extension reports no manifest version;
 * retain that stronger signal for this modal session. */
var _lcDesktopSigLast = null;
var _lcBrowserUpgradeRequested = false;

/* ── Browser-assisted desktop transport (Codelab / web VS Code) ───────
 *
 * A /proxy/<port> URL behind SSO is reachable in this tab but not from the
 * cookieless desktop node.  The node therefore exposes a loopback-only
 * broker on 15180..15189.  This authenticated page carries each desktop poll
 * through the gateway with `credentials: include` and hands the response back
 * to the agent.  Cookies never leave the browser; the local broker accepts
 * only this configured Tofu Origin and answers Private Network Access
 * preflights itself.
 *
 * The loop survives closing this modal and stops naturally when the Tofu tab
 * closes.  The visible button in the SSO hint starts a 30-minute discovery
 * watch; the native panel opens this page with #tofu-agent-relay.  We deliberately do not scan localhost merely because a
 * modal opened — Chromium may show a Local Network Access permission prompt.
 */
var _LC_AGENT_RELAY_PORT_START = 15180;
var _LC_AGENT_RELAY_PORT_END = 15189;
var _lcAgentRelay = { base: '', running: false, state: 'idle', detail: '', watchUntil: 0, watchTimer: null };

function _lcRelayHintText() {
  if (_lcAgentRelay.state === 'connected') {
    return _lcT('local.relayConnected',
      '浏览器安全通道已接通——请保持这个 Tofu 标签页打开。');
  }
  if (_lcAgentRelay.state === 'forwarding') {
    return _lcT('local.relayForwarding',
      '已找到 Tofu 桌面版，正在用当前浏览器的登录态接通……');
  }
  if (_lcAgentRelay.state === 'error') {
    return _lcT('local.relayError',
      '浏览器通道暂时中断，正在自动重连；请确认当前 Tofu 网页仍处于登录状态。');
  }
  if (_lcAgentRelay.state === 'scanning') {
    return _lcT('local.relayScanning',
      '正在本机查找 Tofu 桌面版……');
  }
  if (_lcAgentRelay.state === 'notfound') {
    return _lcT('local.relayNotFound',
      '本机还没有检测到运行中的 Tofu 桌面版——启动它后本页会自动接通。');
  }
  return _lcT('local.relayWaiting',
    '当前地址有 SSO 登录墙，桌面版可能无法直接连入。配对后保持本 Tofu 标签页打开，网页会自动接力连接。');
}

function _lcPaintRelayHint() {
  var el = document.getElementById('lcAgentRelayHint');
  if (el) el.textContent = _lcRelayHintText();
  var btn = document.getElementById('lcAgentRelayStart');
  if (btn) btn.style.display = _lcAgentRelay.state === 'connected' ? 'none' : '';
}

function _lcRelayHintHtml(d) {
  if (!d || d.server_url_reachability !== 'public') return '';
  return '<p class="lc-substep lc-await" id="lcAgentRelayHint">' +
    _lcEsc(_lcRelayHintText()) + '</p>' +
    '<button type="button" class="btn btn-secondary btn-sm" id="lcAgentRelayStart" ' +
    'data-tofu-action="_lcEnsureAgentRelay(1800000)">' +
    _lcEsc(_lcT('local.relayStart', '立即通过浏览器连接')) + '</button>';
}

function _lcLocalRelayFetch(url, options, timeoutMs) {
  var controller = (typeof AbortController !== 'undefined')
    ? new AbortController() : null;
  var opts = Object.assign({ mode: 'cors', credentials: 'omit',
    cache: 'no-store', targetAddressSpace: 'local' }, options || {});
  if (controller) opts.signal = controller.signal;
  var timer = controller ? setTimeout(function () { controller.abort(); },
    timeoutMs || 15000) : null;
  return fetch(url, opts).finally(function () {
    if (timer) clearTimeout(timer);
  });
}

function _lcRelayExpectedUrl(route) {
  var path = (typeof apiUrl === 'function') ? apiUrl(route) : route;
  try { return new URL(path, window.location.href).href; }
  catch (e) { return ''; }
}

function _lcRelayExpectedPollUrl() {
  return _lcRelayExpectedUrl('/api/desktop/poll');
}

function _lcDiscoverAgentRelay() {
  /* Resolves the base URL of the first live broker, else ''. */
  if (typeof fetch !== 'function') return Promise.resolve('');
  var probes = [];
  for (var p = _LC_AGENT_RELAY_PORT_START;
       p <= _LC_AGENT_RELAY_PORT_END; p++) {
    (function (port) {
      var base = 'http://127.0.0.1:' + port;
      probes.push(_lcLocalRelayFetch(base + '/v1/status', {}, 650)
        .then(function (r) {
          if (!r || !r.ok) return null;
          return r.json().then(function (body) {
            return body && body.kind === 'tofu-agent-browser-relay'
              ? base : '';
          });
        }).catch(function () { return ''; }));
    })(p);
  }
  return Promise.all(probes).then(function (rows) {
    for (var i = 0; i < rows.length; i++) if (rows[i]) return rows[i];
    return '';
  });
}

/* The page's own reachable base (origin + proxy prefix) — the ?base= value
 * the pairing link is built on. The proxy strips the prefix before it reaches
 * the server, so only this page knows it; the server pins it to the request's
 * Host. */
function _lcPageBase() {
  try {
    var origin = (window.location && window.location.origin) || '';
    // 'null' (file://, opaque origins) must never become a query value.
    if (/^https?:\/\//.test(origin)) {
      return origin + ((typeof BASE_PATH === 'string') ? BASE_PATH : '');
    }
  } catch (e) { /* no usable origin — the backend falls back to host_url */ }
  return '';
}

function _lcReturnRelayResult(base, id, status, body) {
  return _lcLocalRelayFetch(base + '/v1/result', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: id, status: status, body: body })
  }, 5000);
}

async function _lcAgentRelayLoop(base) {
  if (_lcAgentRelay.running) return;
  _lcAgentRelay.running = true;
  _lcAgentRelay.base = base;
  _lcAgentRelay.state = 'forwarding';
  _lcPaintRelayHint();
  try {
    while (_lcAgentRelay.base === base) {
      var take = await _lcLocalRelayFetch(base + '/v1/take', {}, 13000);
      if (take.status === 204) continue;
      if (!take.ok) throw new Error('local relay HTTP ' + take.status);
      var job = await take.json();
      var expected = _lcRelayExpectedPollUrl();
      /* A node joining through this page redeems its pairing link here: the
       * one-shot secret is the only credential, so no bridge header rides. */
      var expectedPair = _lcRelayExpectedUrl('/api/desktop/pair');
      var target = '';
      try { target = new URL(job.url, window.location.href).href; }
      catch (e) { target = ''; }
      var isPair = !!expectedPair && target === expectedPair;
      if (!isPair && (!expected || target !== expected)) {
        await _lcReturnRelayResult(base, job.id, 409,
          JSON.stringify({ error: 'relay_target_mismatch' }));
        continue;
      }
      try {
        var remote = isPair
          ? await Api.desktop.relayPair(job.payload || {})
          : await Api.desktop.relayPoll(
            job.payload || {},
            job.headers && job.headers['X-Bridge-Secret']);
        var responseBody = await remote.text();
        await _lcReturnRelayResult(base, job.id, remote.status, responseBody);
        _lcAgentRelay.state = remote.ok ? 'connected' : 'error';
        _lcAgentRelay.detail = 'HTTP ' + remote.status;
        _lcPaintRelayHint();
      } catch (remoteErr) {
        await _lcReturnRelayResult(base, job.id, 502,
          JSON.stringify({ error: 'browser_gateway_unreachable' }))
          .catch(function () {});
        _lcAgentRelay.state = 'error';
        _lcAgentRelay.detail = String(remoteErr && remoteErr.message || remoteErr);
        _lcPaintRelayHint();
      }
    }
  } catch (e) {
    _lcAgentRelay.state = 'error';
    _lcAgentRelay.detail = String(e && e.message || e);
    _lcPaintRelayHint();
  } finally {
    _lcAgentRelay.running = false;
    if (_lcAgentRelay.base === base) _lcAgentRelay.base = '';
    if (Date.now() < _lcAgentRelay.watchUntil) {
      setTimeout(_lcRelayWatchBeat, 1000);
    }
  }
}

function _lcRelayWatchBeat() {
  _lcAgentRelay.watchTimer = null;
  if (_lcAgentRelay.running ||
      Date.now() >= _lcAgentRelay.watchUntil) return;
  _lcDiscoverAgentRelay().then(function (base) {
    if (base) {
      _lcAgentRelayLoop(base);
    } else if (Date.now() < _lcAgentRelay.watchUntil) {
      if (_lcAgentRelay.state === 'scanning') {
        /* The click's answer: nothing answered this machine's broker
         * ports. Say so once — the watch keeps running silently, and a
         * later agent start flips the state without another click. */
        _lcAgentRelay.state = 'notfound';
        _lcPaintRelayHint();
      }
      _lcAgentRelay.watchTimer = setTimeout(_lcRelayWatchBeat, 1800);
    }
  });
}

function _lcEnsureAgentRelay(durationMs) {
  var duration = Number(durationMs) || (30 * 60 * 1000);
  _lcAgentRelay.watchUntil = Math.max(_lcAgentRelay.watchUntil,
    Date.now() + duration);
  /* An explicit click must ANSWER. The watch is silent by design (a modal
   * opening must not probe localhost), but without a state change here a
   * user with no agent running yet watches the button do literally
   * nothing — paint the scan now, and the watch paints its empty result. */
  if (_lcAgentRelay.state === 'idle' || _lcAgentRelay.state === 'notfound' ||
      _lcAgentRelay.state === 'error') {
    _lcAgentRelay.state = 'scanning';
    _lcAgentRelay.detail = '';
    _lcPaintRelayHint();
  }
  if (!_lcAgentRelay.running && !_lcAgentRelay.watchTimer) {
    _lcRelayWatchBeat();
  }
}

/* The render inputs that justify a setup-box rewrite. Anything NOT in here
 * changing is not a reason to touch the DOM the user is interacting with
 * (a pairing link being copied, an expanded <details>). The live pairing is
 * painted from module state, so its own transitions repaint explicitly. */
function _lcDesktopSignature(d) {
  function fp(rows) {
    return (Array.isArray(rows) ? rows : []).map(function (p) {
      return [(p && p.filename) || '', (p && p.size) || 0,
              (p && p.version) || '', !!(p && p.older_than_server)].join(':');
    }).join('|');
  }
  var nodes = (Array.isArray(d.nodes) ? d.nodes : []).map(function (n) {
    var tiers = (n && n.tiers) || {};
    return [_lcNodeKey(n), (n && n.name) || '', !!(n && n.online),
            (n && n.platform) || '', (n && n.attachment) || '',
            _LC_TIER_ORDER.map(function (k) { return tiers[k] ? 1 : 0; }).join('')
           ].join(':');
  }).join('|');
  var lang = (typeof _i18nLang !== 'undefined') ? _i18nLang : '';
  return [d.setup_state, !!d.connected, nodes, d.visitor_os || '',
          fp(d.downloads), d.download_url || '', lang,
          d.server_url_reachability || '', d.server_bind || '',
          d.server_version || '', !!d.installers_refreshing
         ].join('~');
}

// ══════════════════════════════════════════════════════
//  Nodes — every machine that joined this server
// ══════════════════════════════════════════════════════

/* Permission tiers in contract order (contracts/desktop_node_v1.schema.json
 * `x-permission-tiers`). A node grants them itself; the panel only shows
 * what it reports. */
var _LC_TIER_ORDER = ['read', 'write', 'exec', 'gui', 'egress', 'codex_login'];

function _lcTierLabel(tier) {
  switch (tier) {
    case 'read': return _lcT('local.tierRead', '读文件');
    case 'write': return _lcT('local.tierWrite', '写文件');
    case 'exec': return _lcT('local.tierExec', '运行命令');
    case 'gui': return _lcT('local.tierGui', '控制鼠标键盘');
    case 'egress': return _lcT('local.tierEgress', '网络转发');
    case 'codex_login': return _lcT('local.tierCodexLogin', 'Codex 登录');
  }
  return tier;
}

/* A node's stable identity: its node id, or — for a paired credential no
 * device has polled with since the server started — the credential id. */
function _lcNodeKey(n) {
  if (!n) return '';
  if (n.node_id) return 'node:' + n.node_id;
  return (n.credential && n.credential.id) ? 'key:' + n.credential.id : '';
}

function _lcNodeTiersText(tiers) {
  var granted = _LC_TIER_ORDER.filter(function (k) {
    return !!(tiers && tiers[k]);
  });
  if (!granted.length) return '—';
  if (granted.length === 1 && granted[0] === 'read') {
    return _lcT('local.tierReadOnly', '只读');
  }
  return granted.map(_lcTierLabel).join(' · ');
}

function _lcPlatformLabel(platform) {
  var p = String(platform || '').toLowerCase();
  if (p.indexOf('win') === 0) return 'Windows';
  if (p === 'darwin' || p.indexOf('mac') === 0) return 'macOS';
  if (p.indexOf('linux') === 0) return 'Linux';
  return platform || '';
}

/* One row per node. The packaged app's in-process node (attachment
 * 'process') is the machine Tofu itself runs on and has no credential to
 * revoke — the server answers 409 — so it carries a hint, not a Remove. */
function _lcNodeRowHtml(n) {
  var online = !!n.online;
  var processNode = n.attachment === 'process';
  var meta = [];
  if (processNode) meta.push(_lcT('local.nodeThisMachine', '本机（运行 Tofu 的电脑）'));
  var os = _lcPlatformLabel(n.platform);
  if (os) meta.push(os);
  meta.push(online ? _lcT('local.nodeOnline', '在线')
                   : _lcT('local.nodeOffline', '离线'));
  var action;
  if (processNode) {
    action = '<span class="lc-node-hint">' + _lcEsc(_lcT('local.nodeProcessHint',
      '在桌面版里关闭「电脑控制」即可停用')) + '</span>';
  } else {
    var keyId = (n.credential && n.credential.id) || '';
    action = '<button type="button" class="btn btn-secondary btn-sm lc-node-remove"' +
      ' data-node-id="' + _lcEsc(n.node_id || '') + '"' +
      ' data-key-id="' + _lcEsc(keyId) + '"' +
      ' data-node-name="' + _lcEsc(n.name || '') + '">' +
      _lcEsc(_lcT('local.nodeRemove', '移除')) + '</button>';
  }
  return '<li class="lc-node' + (online ? ' lc-node-online' : '') + '"' +
      ' data-node-key="' + _lcEsc(_lcNodeKey(n)) + '">' +
    '<span class="browser-status-dot ' + (online ? 'connected' : 'disconnected') + '"></span>' +
    '<span class="lc-node-main">' +
      '<span class="lc-node-name">' + _lcEsc(n.name || n.node_id || '') + '</span>' +
      '<span class="lc-node-meta">' + _lcEsc(meta.join(' · ')) + '</span>' +
      '<span class="lc-node-tiers">' + _lcEsc(_lcNodeTiersText(n.tiers)) + '</span>' +
    '</span>' + action + '</li>';
}

function _lcNodeListHtml(nodes) {
  if (!nodes.length) return '';
  return '<ul class="lc-nodes" id="lcNodeList">' +
    nodes.map(_lcNodeRowHtml).join('') + '</ul>';
}

/* Remove = revoke + disconnect. A live node goes through its node id; an
 * offline paired credential (node_id null) is revoked directly. */
function _lcRemoveNode(btn) {
  if (!btn || btn.disabled) return;
  var nodeId = btn.getAttribute('data-node-id') || '';
  var keyId = btn.getAttribute('data-key-id') || '';
  var name = btn.getAttribute('data-node-name') || nodeId || keyId;
  if (!nodeId && !keyId) return;
  var ask = _lcT('local.nodeRemoveConfirm',
    '移除「{name}」？它会立即断开，需要重新配对才能再连上。').replace('{name}', name);
  if (typeof window !== 'undefined' && typeof window.confirm === 'function' &&
      !window.confirm(ask)) return;
  btn.disabled = true;
  var call = nodeId ? Api.desktop.removeNode(nodeId) : Api.desktop.revokeToken(keyId);
  Promise.resolve(call).then(function () {
    _lcDesktopSigLast = null;
    _lcRefresh();
  }).catch(function () {
    if (btn.isConnected) btn.disabled = false;
    if (typeof showToast === 'function') {
      showToast(_lcT('local.nodeRemoveFailed', '移除失败——稍后再试'));
    }
  });
}

// ══════════════════════════════════════════════════════
//  Pairing — "Add a machine"
// ══════════════════════════════════════════════════════

/* The one live pairing of this page. Minted on an explicit click only (each
 * mint is an audited one-time credential), shown until it expires (ttl from
 * the response, measured on this clock so server clock skew cannot shorten
 * it), and cleared once a node that was not listed at mint time appears. */
var _lcPair = { link: '', code: '', expiresAt: 0, minting: false, error: '', expired: false, joined: '', known: null };
var _lcPairTick = null;
var _lcDesktopLast = null;

function _lcPairActive() {
  return !!_lcPair.link && Date.now() < _lcPair.expiresAt;
}

function _lcFmtCountdown(ms) {
  var s = Math.max(0, Math.ceil(ms / 1000));
  var m = Math.floor(s / 60);
  var r = s % 60;
  return m + ':' + (r < 10 ? '0' : '') + r;
}

function _lcPairCountdownText() {
  return _lcT('local.pairExpiresIn', '{time} 后失效，仅可使用一次')
    .replace('{time}', _lcFmtCountdown(_lcPair.expiresAt - Date.now()));
}

function _lcPairCodeText(code) {
  var c = String(code || '');
  return c.length === 6 ? c.slice(0, 3) + ' ' + c.slice(3) : c;
}

/* The pairing action area: a live link, a mint in flight, or the button. */
function _lcPairActionHtml(secondary) {
  if (_lcPair.minting) {
    return '<p class="lc-substep">' + _lcEsc(_lcT('local.pairMinting',
      '正在生成配对链接……')) + '</p>';
  }
  if (_lcPairActive()) {
    return '<div class="lc-pair-box">' +
      '<span class="lc-pair-label">' + _lcEsc(_lcT('local.pairLinkLabel', '配对链接')) + '</span>' +
      '<div class="lc-pair-link-row">' +
        '<code class="lc-pair-link" id="lcPairLink">' + _lcEsc(_lcPair.link) + '</code>' +
        '<button type="button" class="btn btn-primary btn-sm" id="lcPairCopy">' +
          _lcEsc(_lcT('local.pairCopy', '复制链接')) + '</button>' +
      '</div>' +
      (_lcPair.code
        ? '<p class="lc-substep">' + _lcEsc(_lcT('local.pairCodeLabel',
            '也可以在桌面版里输入配对码：')) +
          ' <strong class="lc-pair-pin" id="lcPairCode">' +
          _lcEsc(_lcPairCodeText(_lcPair.code)) + '</strong></p>'
        : '') +
      '<p class="lc-substep lc-pair-countdown" id="lcPairCountdown">' +
        _lcEsc(_lcPairCountdownText()) + '</p>' +
      '</div>';
  }
  var html = '';
  if (_lcPair.error) {
    html += '<p class="lc-substep lc-warn">' + _lcEsc(_lcT('local.pairFailed',
      '生成配对链接失败：{error}').replace('{error}', _lcPair.error)) + '</p>';
  } else if (_lcPair.expired) {
    html += '<p class="lc-substep">' + _lcEsc(_lcT('local.pairExpired',
      '配对链接已过期，请重新生成。')) + '</p>';
  }
  html += '<button type="button" class="btn ' + (secondary ? 'btn-secondary' : 'btn-primary') +
    ' btn-sm" id="lcPairMintBtn">' + _lcEsc(secondary
      ? _lcT('local.pairAddAnother', '再添加一台电脑')
      : _lcT('local.pairAddBtn', '添加电脑')) + '</button>';
  return html;
}

/* The whole desktop setup box, authored ONCE for the first-frame floor
 * (`d` = an empty pair payload) and every detected state. Connected with no
 * pairing in progress, it is the node list plus a quiet "add another"
 * button; otherwise the one instruction — install the desktop app, choose
 * "Join a server", paste the link — with the visitor-OS installer row. */
function _lcDesktopSetupHtml(d) {
  var nodes = Array.isArray(d.nodes) ? d.nodes : [];
  var guiding = d.setup_state !== 'connected' || _lcPairActive() ||
    _lcPair.minting || !!_lcPair.error;
  var html = _lcNodeListHtml(nodes);
  if (_lcPair.joined) {
    html += '<p class="lc-substep lc-pair-joined">' + _lcEsc(_lcT('local.pairJoined',
      '「{name}」已加入。').replace('{name}', _lcPair.joined)) + '</p>';
  }
  html += '<div class="lc-pair" id="lcPairArea">';
  if (guiding) {
    html += _lcBindWarnHtml(d) + _lcRelayHintHtml(d) +
      '<p class="lc-step">' + _lcEsc(_lcT('local.pairLead',
        '让 AI 操作一台电脑：在那台电脑上安装 Tofu 桌面版，打开后选择「加入服务器」，粘贴配对链接。')) + '</p>' +
      _lcDownloadLinks(d);
  }
  html += _lcPairActionHtml(!guiding) + '</div>';
  if (d.setup_state === 'pair' && d.connected === false) html += _lcDiagInboxHtml();
  return html;
}

function _lcWireDesktop(setup) {
  var mint = document.getElementById('lcPairMintBtn');
  if (mint) mint.onclick = _lcMintPairing;
  var copy = document.getElementById('lcPairCopy');
  if (copy) {
    copy.onclick = function () {
      if (typeof _safeClipboardWrite !== 'function') return;
      Promise.resolve(_safeClipboardWrite(_lcPair.link)).then(function () {
        copy.textContent = _lcT('local.pairCopied', '已复制');
      }).catch(function () {});
    };
  }
  var removes = setup.querySelectorAll('button.lc-node-remove');
  for (var i = 0; i < removes.length; i++) {
    removes[i].onclick = function (ev) { _lcRemoveNode(ev.currentTarget); };
  }
  if (document.getElementById('lcDiagSubmit')) _lcWireDiag();
}

/* Repaint the setup box from the last status payload (or the floor). */
function _lcRepaintDesktop() {
  var setup = document.getElementById('lcDesktopSetup');
  if (!setup) return;
  _lcDesktopSigLast = null;
  setup.innerHTML = _lcDesktopSetupHtml(_lcDesktopLast ||
    { setup_state: 'pair', nodes: [] });
  _lcWireDesktop(setup);
}

function _lcStopPairTick() {
  if (_lcPairTick) { clearInterval(_lcPairTick); _lcPairTick = null; }
}

function _lcStartPairTick() {
  _lcStopPairTick();
  if (!_lcPairActive()) return;
  _lcPairTick = setInterval(function () {
    if (!_lcPairActive()) {
      _lcStopPairTick();
      _lcPair.link = '';
      _lcPair.code = '';
      _lcPair.expired = true;
      _lcRepaintDesktop();
      return;
    }
    var el = document.getElementById('lcPairCountdown');
    if (el) el.textContent = _lcPairCountdownText();
  }, 1000);
}

function _lcMintPairing() {
  if (_lcPair.minting) return;
  if (typeof Api === 'undefined' || !Api.desktop ||
      typeof Api.desktop.pairCode !== 'function') return;
  var known = ((_lcDesktopLast && Array.isArray(_lcDesktopLast.nodes))
    ? _lcDesktopLast.nodes : []).map(_lcNodeKey);
  _lcPair = { link: '', code: '', expiresAt: 0, minting: true, error: '',
              expired: false, joined: '', known: known };
  _lcRepaintDesktop();
  Promise.resolve(Api.desktop.pairCode(_lcPageBase())).then(function (r) {
    if (!r || !r.link) throw new Error('empty pairing response');
    _lcPair.minting = false;
    _lcPair.link = String(r.link);
    _lcPair.code = String(r.code || '');
    _lcPair.expiresAt = Date.now() + (Number(r.ttl) || 300) * 1000;
    _lcRepaintDesktop();
    _lcStartPairTick();
    /* Behind an SSO edge the node cannot reach this server by itself; it
     * redeems the link through this page's local relay, so watch for it for
     * as long as the link lives. */
    if (_lcDesktopLast && _lcDesktopLast.server_url_reachability === 'public') {
      _lcEnsureAgentRelay((Number(r.ttl) || 300) * 1000);
    }
  }).catch(function (e) {
    _lcPair.minting = false;
    _lcPair.error = String((e && e.message) || e || '');
    _lcRepaintDesktop();
  });
}

/* A node that was not listed when the link was minted means the pairing was
 * redeemed: retire the link and name the machine that joined. */
function _lcNoticeJoinedNode(nodes) {
  if (!_lcPairActive() || !Array.isArray(_lcPair.known)) return;
  for (var i = 0; i < nodes.length; i++) {
    var key = _lcNodeKey(nodes[i]);
    if (key && _lcPair.known.indexOf(key) < 0) {
      _lcStopPairTick();
      _lcPair = { link: '', code: '', expiresAt: 0, minting: false, error: '',
                  expired: false, joined: nodes[i].name || nodes[i].node_id || '',
                  known: null };
      _lcDesktopSigLast = null;
      return;
    }
  }
}

/* ── The diagnostics inbox (owner ask 2026-08-06) ──
 * A controlled machine that cannot reach this server cannot push its logs
 * anywhere — debugging it blind was the 2026-08-06 incident's blind spot.
 * The agent's window/tray has「复制诊断信息」; the user pastes the bundle
 * HERE and it lands in logs/desktop_client_diag.log on the server, where
 * the operator/assistant reads it directly. Collapsed by default so it
 * never competes with the ONE primary install action. */
function _lcDiagInboxHtml() {
  return '<details class="lc-details lc-diag"><summary class="ui-disclosure">' +
    _lcEsc(_lcT('local.diagTitle', '电脑连不上？把桌面版的诊断信息粘贴到这里')) +
    '</summary>' +
    '<p class="lc-substep">' + _lcEsc(_lcT('local.diagDesc',
      '在 Tofu 桌面版里点「复制诊断信息」（或托盘菜单同名项），回到这里粘贴提交——服务器会直接存盘，排查时立刻能读到，不用截图不用转述。')) + '</p>' +
    '<textarea id="lcDiagText" class="lc-diag-text" rows="6" spellcheck="false" placeholder="' +
    _lcEsc(_lcT('local.diagPlaceholder', 'Ctrl+V 粘贴诊断信息……')) + '"></textarea>' +
    '<div class="lc-diag-actions">' +
    '<button type="button" id="lcDiagSubmit" class="btn btn-primary btn-sm">' +
    _lcEsc(_lcT('local.diagSubmit', '提交诊断')) + '</button>' +
    '<span id="lcDiagHint" class="lc-diag-hint"></span></div>' +
    '<div id="lcDiagRecent" class="lc-diag-recent"></div>' +
    '</details>';
}

function _lcDiagRefreshRecent() {
  var box = document.getElementById('lcDiagRecent');
  if (!box) return;
  // jsdom harnesses splice the renderers without an Api stub — the
  // refresh is best-effort, never a render-time throw.
  if (typeof Api === 'undefined' || !Api.desktop ||
      typeof Api.desktop.listDiags !== 'function') return;
  Promise.resolve(Api.desktop.listDiags()).then(function (r) {
    var boxNow = document.getElementById('lcDiagRecent');
    if (!boxNow) return;
    if (!r || !Array.isArray(r.entries) || !r.entries.length) {
      boxNow.innerHTML = '';
      return;
    }
    var rows = r.entries.slice(0, 5).map(function (e) {
      var when = e.ts ? new Date(e.ts * 1000).toLocaleString() : '?';
      return '<div class="lc-diag-row">' + _lcEsc(when) + ' · ' +
        _lcEsc(String(e.chars || 0)) + ' chars</div>';
    }).join('');
    boxNow.innerHTML = '<div class="lc-diag-recent-head">' +
      _lcEsc(_lcT('local.diagRecent', '最近提交')) + '</div>' + rows;
  });
}

function _lcWireDiag() {
  var btn = document.getElementById('lcDiagSubmit');
  if (!btn) return;
  btn.addEventListener('click', function () {
    var ta = document.getElementById('lcDiagText');
    var hint = document.getElementById('lcDiagHint');
    var text = ta ? ta.value.trim() : '';
    if (!text) {
      if (hint) hint.textContent = _lcT('local.diagEmpty', '先粘贴诊断信息再提交');
      return;
    }
    btn.disabled = true;
    Promise.resolve(Api.desktop.submitDiag(text)).then(function (r) {
      btn.disabled = false;
      if (!r) {
        if (hint) hint.textContent = _lcT('local.diagFailed', '提交失败——稍后再试');
        return;
      }
      if (ta) ta.value = '';
      if (hint) hint.textContent = _lcT('local.diagDone', '已收到——服务器已存盘，可以去排查了');
      _lcDiagRefreshRecent();
    });
  });
  _lcDiagRefreshRecent();
}

/* A loopback-bound server cannot be reached by any other machine: a node
 * dials OUT to the server it joined, and a 127.0.0.1 bind refuses every
 * off-machine route the pairing link names. Measured 2026-08-05: a
 * platform-injected BIND_HOST=127.0.0.1 quietly overrode the 0.0.0.0
 * default and attach failed silently. The panel says so out loud — the one
 * operator action that actually unblocks pairing. */
function _lcBindWarnHtml(d) {
  if (!d || d.server_bind !== 'loopback') return '';
  return '<p class="lc-substep lc-warn">' + _lcEsc(_lcT('local.bindLoopbackWarn',
    '⚠️ 服务器当前只监听本机回环（BIND_HOST=127.0.0.1）——其他电脑无法直接连入；去掉该环境变量并重启后，配对的电脑才能连上。')) + '</p>';
}

function openLocalControlModal(options) {
  var el = document.getElementById('localControlModal');
  if (!el) return;
  _lcBrowserUpgradeRequested = !!(options && options.browserUpgrade);
  el.classList.add('open');
  _lcDesktopSigLast = null;
  _lcPaintFloor();
  _lcStartPairTick();
  _lcRefresh();
  if (_lcPollTimer) clearInterval(_lcPollTimer);
  _lcPollTimer = setInterval(_lcRefresh, _LC_POLL_MS);
}

/* Put a real, followable instruction in BOTH rows before anything is fetched.
 *
 * Runs synchronously on open, so the first frame the user sees already tells
 * them what to do. Everything here is derivable with ZERO backend knowledge:
 * downloading the extension ZIP and "install the desktop app, then paste a
 * pairing link" hold whatever the probe later reports — and the Add-a-machine
 * button works before any status answer. The installer row waits for the
 * backend (its URL derives from UPDATE_REPO; a hardcoded one would point a
 * fork at the wrong releases page). The renderers then narrow this to the
 * state-specific view: load-unpacked with the on-disk path, or the node list.
 *
 * Status text is painted too. It reads "not installed" / "not running" rather
 * than "checking": for a user who has not set this up — the only user who
 * needs this dialog — that is both the honest answer and the one the poll is
 * about to confirm, and it does not go stale if the poll never answers. */
function _lcPaintFloor() {
  _lcSetStatus('lcBrowserStatus', false, _lcT('local.notInstalled', '尚未安装'));
  _lcSetStatus('lcDesktopStatus', false, _lcT('local.notRunning', '未运行'));
  _lcBrowserDownload();
  var setup = document.getElementById('lcDesktopSetup');
  if (setup) {
    setup.innerHTML = _lcDesktopSetupHtml({ setup_state: 'pair', nodes: [] });
    _lcWireDesktop(setup);
  }
}

function closeLocalControlModal() {
  var el = document.getElementById('localControlModal');
  if (el) el.classList.remove('open');
  _lcBrowserUpgradeRequested = false;
  if (_lcPollTimer) { clearInterval(_lcPollTimer); _lcPollTimer = null; }
  _lcStopPairTick();
}

/* The architecture THIS machine runs, as reported by the browser itself.
 *
 * `navigator.userAgentData.getHighEntropyValues(['architecture'])` is the only
 * practical source of this fact. The UA string cannot supply it on macOS — an
 * Apple Silicon Mac reports "Intel Mac OS X", Chrome and Safari alike — and
 * the `Sec-CH-UA-Arch` request header is sent only AFTER a server has already
 * answered once with an `Accept-CH` opt-in, so the very first page load (the
 * one that renders the download button) would be arch-blind.
 *
 * `null` while unresolved and `''` when the browser refuses to say — both mean
 * "do not narrow", and the backend then returns BOTH macOS DMGs. That ambiguous
 * answer is CORRECT: guessing wrong hands the user a download that cannot open.
 * Resolved once per page (the answer cannot change) and never awaited by the
 * paint path, so a browser without the API costs nothing. */
var _lcArch = null;

function _lcResolveArch() {
  if (_lcArch !== null) return Promise.resolve(_lcArch);
  var uad = (typeof navigator !== 'undefined') ? navigator.userAgentData : null;
  if (!uad || typeof uad.getHighEntropyValues !== 'function') {
    _lcArch = '';
    return Promise.resolve(_lcArch);
  }
  return Promise.resolve(uad.getHighEntropyValues(['architecture']))
    .then(function (v) {
      _lcArch = (v && v.architecture) ? String(v.architecture) : '';
      return _lcArch;
    })
    .catch(function () { _lcArch = ''; return _lcArch; });
}

/* Fetch both capabilities' state and repaint. Each side is independent —
 * one backend hiccup must not blank the other row. */
function _lcRefresh() {
  if (typeof Api === 'undefined' || !Api.browser || !Api.desktop) return;
  Promise.resolve(Api.browser.status())
    .then(_lcRenderBrowser)
    .catch(function (e) { _lcRenderBrowser(null, e); });
  _lcResolveArch().then(function (arch) {
    return Promise.resolve(Api.desktop.status(arch))
      .then(_lcRenderDesktop)
      .catch(function (e) { _lcRenderDesktop(null, e); });
  });
}

/* The toolbar row presents _lcReach after this modal closes, when the poll
 * that maintains it has stopped. The retained badge calls this when the row is
 * about to be seen, so the row never shows a closed session's answer. It
 * writes reachability and the row only: the closed modal is not repainted, so
 * a menu click cannot raise a join toast. A failed call leaves the last
 * confirmed value, as the renderers do. */
function _lcProbeReach() {
  if (_lcPollTimer || typeof Api === 'undefined' || !Api.browser || !Api.desktop) return;
  Promise.resolve(Api.browser.status()).then(function (d) {
    if (!d) return;
    _lcReach.browser = !!d.connected;
    _lcUpdateBadge();
  }, function () {});
  _lcResolveArch().then(function (arch) {
    return Api.desktop.status(arch);
  }).then(function (d) {
    if (!d) return;
    _lcReach.desktop = !!d.connected;
    _lcUpdateBadge();
  }, function () {});
}

function _lcT(key, fallback) {
  if (typeof t === 'function') {
    var v = t(key);
    if (v && v !== key) return v;
  }
  return fallback;
}

function _lcEsc(s) {
  if (typeof escapeHtml === 'function') return escapeHtml(String(s == null ? '' : s));
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* Paint one row's dot + label. */
function _lcSetStatus(rowId, connected, label) {
  var box = document.getElementById(rowId);
  if (!box) return;
  var dot = box.querySelector('.browser-status-dot');
  if (dot) {
    dot.classList.toggle('connected', !!connected);
    dot.classList.toggle('disconnected', !connected);
  }
  var txt = box.querySelector('.lc-status-text');
  if (txt) txt.textContent = label;
}

/* Paint one row's switch (reflects the real wire flag, not modal-local state).
 *
 * `reachable` gates turning the capability ON. Switching it on while nothing
 * is connected is the ORIGINAL silent-failure bug in a new costume:
 * `lib/tools/registry/_build.py` ships ZERO tools for an unconnected bridge,
 * so the toggle would light up and the AI would still have nothing. A control
 * that cannot achieve what it claims must not invite the click. Once the agent
 * connects the live poll re-enables it within one beat, so it is never a dead
 * end.
 *
 * ── The gate is ONE-WAY, and that asymmetry is the point ──
 * Turning OFF is ALWAYS allowed, even while disconnected. Gating both
 * directions meant a capability enabled while the agent was up became
 * unrevokable the moment that agent dropped: the flag stayed ON on the wire
 * (it persists per-conversation and is sent to the server), the switch showed
 * ON and greyed out, and the one action a worried user wants — withdraw access
 * to their own machine — was the one action the UI refused. A safety control
 * must never be harder to switch off than on. */
function _lcSetSwitch(switchId, on, reachable) {
  var sw = document.getElementById(switchId);
  if (!sw) return;
  var canEnable = (reachable === undefined) ? true : !!reachable;
  var can = canEnable || !!on;   // already on ⇒ always revocable
  sw.classList.toggle('on', !!on);
  sw.setAttribute('aria-checked', on ? 'true' : 'false');
  sw.disabled = !can;
  sw.classList.toggle('lc-switch-off', !can);
  /* Flag a capability that is ON while nothing is connected: the AI is getting
   * zero tools from it, so leaving it looking healthy repeats the original
   * lie in a quieter form. */
  sw.classList.toggle('lc-switch-stale', !!on && !canEnable);
  if (!can) {
    sw.title = _lcT('local.switchBlocked',
      '连接成功后才能开启 —— 现在打开，AI 也拿不到任何工具。');
  } else if (!!on && !canEnable) {
    sw.title = _lcT('local.switchStale',
      '已开启，但当前未连接 —— AI 现在拿不到这项能力的任何工具。可随时关闭。');
  } else {
    sw.removeAttribute('title');
  }
}

/* Render the "what does this actually give the AI" line for one row.
 *
 * Users are being asked to grant real access to their browser session and
 * their machine; "Browser tabs / This computer" alone does not let them make
 * that call. Kept to ONE short line per row — a full tool list would be the
 * menu-of-everything this merge exists to remove — and phrased as concrete
 * actions rather than tool names, since the tool names are an implementation
 * detail the user never types. */
function _lcSetAbout(rowId, text) {
  var host = document.getElementById(rowId);
  if (!host) return;
  host.textContent = text;
}

// ══════════════════════════════════════════════════════
//  Browser tabs
// ══════════════════════════════════════════════════════

/* Which ONE instruction the browser row shows.
 *
 * BOTH inputs come from the backend's own detection, and BOTH are required
 * for the 'load_unpacked' branch:
 *
 *   - `extensionPath` — routes/api_v1/browser.py fills it only for a
 *     loopback peer whose machine also has a drivable browser.
 *   - `localBrowser` — the probe result: which Chromium-family browser this
 *     machine actually has, or null.
 *
 * ── Why the probe and not the path alone ──
 * This branch used to key off `extensionPath` only, and that is exactly how
 * the dead button shipped. The path's own gate was a pure IP test, which a
 * same-host reverse proxy makes vacuously true for public traffic, so a
 * remote user got a button whose click opened a browser window on a headless
 * server — three 404s in the log and no way for the user to tell why. The
 * probe is a fact about the machine that no proxy can forge.
 *
 * Keeping the `&& localBrowser` conjunction here (rather than trusting the
 * backend to have already ANDed them) is deliberate: it is the frontend's own
 * statement of the rule this file exists to enforce, and it means a future
 * payload that carries a path without a browser still cannot produce a button
 * that has nothing to open. */
function _lcBrowserSetupState(d) {
  if (d && d.connected) return 'connected';
  if (d && d.extensionPath && d.localBrowser) return 'load_unpacked';
  return 'download';
}

function _lcRenderBrowser(d, err) {
  var setup = document.getElementById('lcBrowserSetup');
  var connected = !!(d && d.connected);

  if (err || !d) {
    _lcSetStatus('lcBrowserStatus', false, _lcT('local.unreachable', '无法连接服务器'));
    // Falls back to the download instruction, NOT an empty box. Losing the
    // status call says nothing about whether the user needs the extension —
    // and it is precisely when the backend is flaky that wiping the one
    // followable step off the screen is least defensible.
    _lcBrowserDownload();
    return;
  }

  var clients = d.clients || [];

/* Which connected client should automation go to? Prefer the extension that
 * stamped THIS document (origin_marker.js, data-tofu-browser-bridge) — with
 * two computers on one account, clients[0] is whatever polled first and can
 * be the OTHER machine's browser, which is how clicks land on a screen the
 * user is not looking at. Unknown/absent marker falls back to clients[0]. */
function _lcLocalBrowserClientId(clients) {
  var localId = (typeof document !== 'undefined' && document.documentElement)
    ? (document.documentElement.getAttribute('data-tofu-browser-bridge') || '')
    : '';
  if (localId) {
    for (var i = 0; i < clients.length; i++) {
      if (clients[i] && clients[i].client_id === localId) return localId;
    }
  }
  return clients[0].client_id;
}
  /* Fleet recovery states: the server reports
   * the version a fresh download would carry (servedExtVersion) and the
   * clients whose polls DIED at the bridge gate (lockedOutClients) or strict
   * protocol handshake (incompatibleClients). A
   * stale-but-connected extension still works — the dot stays green and
   * the upgrade is a one-click nudge. A locked-out extension cannot poll
   * at all, so calling it "尚未安装" would be a lie: it is installed,
   * broken, and unable to heal itself — the row must say so and offer
   * the preseeded re-download (zero-config cure). */
  var servedVer = ((d && d.servedExtVersion) || '').trim();
  var staleFrom = '';
  if (connected && servedVer && clients.length) {
    /* Only THIS device's extension decides whether the nudge fires. Scanning
     * the whole fleet means a stale side-load on the OTHER computer shows an
     * "upgrade your extension" nudge here even after this one is current — a
     * nag about a device the user is not looking at. The local client is the
     * one that stamped this document (same resolution as automation routing);
     * absent a marker, clients[0] stands in for this device. */
    var localId = _lcLocalBrowserClientId(clients);
    var _lv = '';
    for (var _ci = 0; _ci < clients.length; _ci++) {
      if (clients[_ci] && clients[_ci].client_id === localId) {
        _lv = ((clients[_ci].ext_version) || '').trim();
        break;
      }
    }
    if (_lv && _lv !== servedVer) staleFrom = _lv;
  }
  var lockedOut = (!connected && d && Array.isArray(d.lockedOutClients))
    ? d.lockedOutClients : [];
  var incompatible = (!connected && d && Array.isArray(d.incompatibleClients))
    ? d.incompatibleClients : [];
  if (connected) {
    var ago = (d.secondsAgo != null) ? d.secondsAgo + 's' : '';
    if (clients.length > 0) {
      runtimeScope._browserClientId = _lcLocalBrowserClientId(clients);
    }
    /* Name WHICH extension binary is connected, next to the dot — the
     * version is the only at-a-glance evidence of a stale side-load. Only a
     * fleet-wide agreement earns the suffix: mixed versions stay unnamed here
     * because the upgrade nudge below is what names the stale install, and a
     * legacy client reporting no manifest version yields an empty set. */
    var extVers = [];
    for (var _vi = 0; _vi < clients.length; _vi++) {
      var _vv = String((clients[_vi] && clients[_vi].ext_version) || '').trim();
      if (_vv && extVers.indexOf(_vv) === -1) extVers.push(_vv);
    }
    var verSuffix = extVers.length === 1 ? ' · v' + extVers[0] : '';
    _lcSetStatus('lcBrowserStatus', true,
      clients.length > 1
        ? _lcT('local.connectedN', '已连接').replace('{n}', clients.length) + verSuffix
        : _lcT('local.connected', '已连接') + verSuffix + (ago ? ' · ' + ago : ''));
  } else {
    runtimeScope._browserClientId = null;
    _lcSetStatus('lcBrowserStatus', false,
      lockedOut.length
        ? _lcT('local.extDead', '已安装但凭证已失效')
        : incompatible.length
          ? _lcT('local.extUpgradeRequired', '已安装但需要升级')
        : _lcT('local.notInstalled', '尚未安装'));
  }

  _lcReach.browser = connected;
  _lcUpdateBadge();
  _lcSetAbout('lcBrowserAbout', _lcT('local.browserAbout',
    '读取你已打开的标签页内容，并代你点击、填表单、切换页面。'));

  // Chrome 142+ LNA guidance stays keyed on the CONNECTED extension's version.
  if (typeof _applyBrowserLnaWarning === 'function') {
    _applyBrowserLnaWarning(d.chromeMajor);
  }

  if (!setup) return;
  var state = _lcBrowserSetupState(d);
  if (state === 'connected') {
    if (staleFrom || _lcBrowserUpgradeRequested) {
      // Outdated but WORKING: nothing is broken, so this is a nudge, not
      // an alarm — one click on the preseeded zip upgrades in place. A
      // capability-triggered entry has stronger evidence than version diff:
      // legacy extensions often report no ext_version at all.
      var upgradeLead = staleFrom
        ? _lcT('local.browserExtOutdated',
          '扩展有新版本（{old} → {new}）——重新下载 ZIP，覆盖解压到当初加载的那个文件夹即可：扩展发现新文件会自动重载完成升级（已自动配对，零配置）：')
          .replace('{old}', staleFrom).replace('{new}', servedVer)
        : _lcT('local.browserExtCapabilityMismatch',
          '当前扩展已连接，但缺少站点适配器所需能力。重新下载 ZIP，覆盖解压到当初加载的文件夹，扩展会自动重载完成升级：');
      setup.innerHTML = '<p class="lc-step">' + _lcEsc(upgradeLead) + '</p>' +
        _lcExtDownloadAction();
      _lcWireExtDownload();
    } else {
      setup.innerHTML = '';
    }
    return;
  }

  if (lockedOut.length) {
    // Stranded: an installed extension is knocking with a dead credential.
    // This takes precedence over the load_unpacked/download guidance —
    // the user's problem is not getting the folder, it is replacing a
    // broken install with the self-pairing one.
    setup.innerHTML =
      '<p class="lc-step">' + _lcEsc(_lcT('local.browserExtStranded',
        '检测到旧版扩展因凭证失效连不上（它自己无法恢复）——重新下载扩展 ZIP（已自动配对、零配置），覆盖解压到当初加载的文件夹，扩展自动重载即恢复：')) + '</p>' +
      _lcExtDownloadAction();
    _lcWireExtDownload();
    return;
  }

  if (incompatible.length) {
    // The owner/device authenticated successfully, so re-pairing is the wrong
    // cure. It was rejected before registration because its protocol is old;
    // name that exact state and offer the current binary.
    var rejected = incompatible[0] || {};
    var reportedProtocol = rejected.protocol_version || '?';
    var requiredProtocol = (d && d.protocolVersion) || '?';
    setup.innerHTML =
      '<p class="lc-step">' + _lcEsc(_lcT('local.browserExtProtocolMismatch',
        '检测到浏览器扩展协议过旧（v{old} → v{new}），后端已安全拒绝命令。重新下载扩展 ZIP，覆盖解压到当初加载的文件夹，扩展自动重载即可恢复：')
        .replace('{old}', reportedProtocol).replace('{new}', requiredProtocol)) + '</p>' +
      _lcExtDownloadAction();
    _lcWireExtDownload();
    return;
  }

  if (state === 'load_unpacked') {
    // Tofu runs on this machine, this machine HAS a browser we can drive, and
    // the unpacked extension is already on disk. One primary action: that
    // button (it also copies the path). What remains — Developer mode, Load
    // unpacked, paste — is inside the browser's sandbox and no web page can
    // do it for the user; the text says so instead of implying one click
    // finishes the install.
    //
    // The browser is named from the PROBE, never hardcoded: ordering an Edge
    // user into Chrome is its own dead instruction.
    var lb = d.localBrowser || {};
    var bname = lb.name || 'Chrome';
    setup.innerHTML =
      '<button type="button" class="btn btn-primary btn-sm" id="lcExtOpenBtn">' +
        _lcEsc(_lcT('local.browserOpenPageBtn',
          '帮我打开扩展管理页（自动复制路径）')) + '</button>' +
      '<p class="lc-step">' + _lcEsc(
        _lcT('local.browserLoadUnpacked',
          '剩下的三步 {browser} 不允许网页代劳：① 打开右上角「开发者模式」→ ② 点「加载已解压的扩展程序」→ ③ 粘贴路径（已自动复制）选择这个文件夹：')
        .replace('{browser}', bname)) + '</p>' +
      '<code class="lc-copy" id="lcExtPath" data-tooltip="' +
        _lcEsc(_lcT('browser.clickToCopy', '点击复制')) + '">' +
        _lcEsc(d.extensionPath) + '</code>' +
      '<p class="lc-substep" id="lcExtOpenNote"></p>';
    var openBtn = document.getElementById('lcExtOpenBtn');
    if (openBtn) {
      openBtn.onclick = function () { _lcOpenExtensionsPage(d.extensionPath); };
    }
    var code = document.getElementById('lcExtPath');
    if (code) {
      code.onclick = function () {
        if (typeof _safeClipboardWrite === 'function') {
          _safeClipboardWrite(d.extensionPath)
            .then(function () { code.classList.add('copied'); })
            .catch(function () {});
        }
      };
    }
    return;
  }

  // The remaining case — either the folder does not exist on the user's
  // machine (remote server), or this machine has no browser we could drive,
  // which means the user is not sitting at it either. Both reduce to the same
  // ONE actionable path: download the ZIP, then load it in YOUR browser.
  //
  // This branch is load-bearing beyond the remote case now: it is what the
  // panel falls through to instead of rendering a button that can only 404,
  // and what the pre-detection floor shows on open — hence one shared
  // authoring in _lcBrowserDownload rather than markup inline here.
  // An empty panel would be worse than a wrong instruction.
  _lcBrowserDownload();
}

/* The download-the-ZIP instruction — authored ONCE.
 *
 * Shown in three situations that are the same instruction: the pre-detection
 * floor, a failed status call, and the detected `download` state. It needs no
 * payload (downloadBrowserExtension is a pure frontend call), which is exactly
 * what makes it usable as the floor. */
/* The download BUTTON — authored once, shared by the plain install
 * instruction, the upgrade nudge and the stranded-rescue branch (three
 * copies of a button would drift; a drifted button is a dead one). */
function _lcExtDownloadAction() {
  return '<button type="button" class="btn btn-primary btn-sm" id="lcExtDownloadBtn">' +
    _lcEsc(_lcT('browser.stepDownloadBtn', '下载扩展 ZIP')) + '</button>';
}

function _lcWireExtDownload() {
  var btn = document.getElementById('lcExtDownloadBtn');
  if (btn) {
    btn.onclick = function () {
      if (typeof downloadBrowserExtension === 'function') downloadBrowserExtension();
    };
  }
}

function downloadBrowserExtension() {
  // Carry the browser's OWN base (origin + live BASE_PATH, e.g. /proxy/15000
  // behind a cloud-IDE gateway) so the zip's bridge_preseed pairs the
  // extension with an address this browser demonstrably reaches. A
  // server-side request.host_url loses both external https and proxy prefix.
  var base = encodeURIComponent(window.location.origin + BASE_PATH);
  window.open(apiUrl('/api/browser/download?base=' + base), '_blank');
}

/* Chrome 142+ Local Network Access prompts can fire per site during multi-tab
 * work. Only the Local Control browser card renders this recovery guidance. */
function _applyBrowserLnaWarning(chromeMajor) {
  var box = document.getElementById('browserLnaWarning');
  if (!box) return;
  if (!chromeMajor || chromeMajor < 142) {
    box.style.display = 'none';
    return;
  }
  box.style.display = '';
  var pol = document.getElementById('browserLnaPolicy');
  if (pol && !pol._wired) {
    pol._wired = true;
    pol.onclick = function () {
      if (typeof _safeClipboardWrite === 'function') {
        _safeClipboardWrite(pol.textContent)
          .then(function () { pol.classList.add('copied'); })
          .catch(function () {});
      }
    };
  }
  var pathEl = document.getElementById('browserLnaPath');
  if (pathEl) {
    var ua = (navigator.userAgent || '').toLowerCase();
    var dir = '';
    if (ua.indexOf('windows') >= 0) {
      dir = 'HKLM\\SOFTWARE\\Policies\\Google\\Chrome\\ (via registry / Group Policy)';
    } else if (ua.indexOf('mac os') >= 0 || ua.indexOf('macintosh') >= 0) {
      dir = "defaults write com.google.Chrome LocalNetworkAccessAllowedForUrls -array '*'";
    } else {
      dir = '/etc/opt/chrome/policies/managed/tofu-lna.json';
    }
    var label = (typeof t === 'function')
      ? t('browser.lnaPathLabel') : 'Place it at:';
    pathEl.style.display = '';
    pathEl.innerHTML = label + ' <code>' +
      dir.replace(/</g, '&lt;') + '</code>';
  }
}

function _lcBrowserDownload() {
  var setup = document.getElementById('lcBrowserSetup');
  if (!setup) return;
  setup.innerHTML =
    '<p class="lc-step">' + _lcEsc(_lcT('local.browserDownload',
      '下载扩展并解压，然后在 Chrome / Edge 里打开扩展管理页 → 开启「开发者模式」→「加载已解压的扩展程序」→ 选择解压出的文件夹。')) + '</p>' +
    _lcExtDownloadAction();
  _lcWireExtDownload();
}

// ══════════════════════════════════════════════════════
//  Computers (nodes)
// ══════════════════════════════════════════════════════

function _lcRenderDesktop(d, err) {
  var setup = document.getElementById('lcDesktopSetup');
  if (err || !d) {
    _lcSetStatus('lcDesktopStatus', false, _lcT('local.unreachable', '无法连接服务器'));
    // Keep whatever instruction is on screen (the floor, or the last good
    // state) rather than blanking to an empty box — see _lcPaintFloor.
    return;
  }

  var nodes = Array.isArray(d.nodes) ? d.nodes : [];
  var onlineCount = nodes.filter(function (n) { return n && n.online; }).length;
  var connected = !!d.connected;
  _lcSetStatus('lcDesktopStatus', connected,
    connected
      ? (onlineCount > 1
        ? _lcT('local.connectedN', '已连接 {n} 个').replace('{n}', onlineCount)
        : _lcT('local.connected', '已连接'))
      : _lcT('local.notRunning', '未运行'));
  _lcReach.desktop = connected;
  _lcSetSwitch('lcDesktopSwitch',
    LocalControlShellState.desktopEnabled, connected);
  _lcUpdateBadge();
  _lcSetAbout('lcDesktopAbout', _lcT('local.desktopAbout',
    '浏览与读写本机文件、截屏、打开应用、运行命令（写入与执行需单独授权）。'));

  /* The permission note explains where a node's tiers are granted (in the
   * desktop app on that machine). It only means something once a node is
   * listed — next to nothing it is an instruction the user cannot follow. */
  var perm = document.getElementById('lcPermNote');
  if (perm) perm.style.display = nodes.length ? '' : 'none';

  _lcDesktopLast = d;
  _lcNoticeJoinedNode(nodes);
  if (!setup) return;

  /* ── Poll-signature gate (owner-measured 2026-08-03) ──
   * _lcRefresh repaints every 3s so a freshly-paired node appears at once —
   * but rewriting setup.innerHTML on every beat also blew away the USER's
   * interaction state: an expanded <details> collapsed seconds after
   * opening. Rewrite only when the render INPUTS changed; the dot/text/
   * switch above still update every beat. */
  var sig = _lcDesktopSignature(d);
  if (sig === _lcDesktopSigLast) return;
  _lcDesktopSigLast = sig;

  // The backend chose the state — see routes/api_v1/desktop.py::_setup_state.
  setup.innerHTML = _lcDesktopSetupHtml(d);
  _lcWireDesktop(setup);
}


/* The ONE action of the on-disk browser case: ask the server to open this
 * machine's browser at its extensions page, and copy the extension path from
 * the FRONTEND (navigator.clipboard — a headless server has no clipboard, so
 * this half must happen here). Both fire together. The three remaining clicks
 * live inside the browser's sandbox — the note never claims the install is
 * finished.
 *
 * This handler is only reachable when the backend probe already found a
 * drivable browser (see _lcBrowserSetupState), so "no browser installed" is
 * no longer one of the outcomes it has to explain — that case never renders
 * the button in the first place. What remains is a genuine launch failure, so
 * the note says what to do by hand rather than guessing at a cause. */
function _lcOpenExtensionsPage(path) {
  var btn = document.getElementById('lcExtOpenBtn');
  var note = document.getElementById('lcExtOpenNote');
  if (btn) btn.disabled = true;
  var copied = (path && typeof _safeClipboardWrite === 'function')
    ? Promise.resolve(_safeClipboardWrite(path)).catch(function () {})
    : Promise.resolve();
  var opened = (typeof Api !== 'undefined' && Api.browser &&
                typeof Api.browser.openExtensions === 'function')
    ? Promise.resolve(Api.browser.openExtensions()).catch(function () { return null; })
    : Promise.resolve(null);
  Promise.all([copied, opened]).then(function (results) {
    if (btn) btn.disabled = false;
    var r = results[1];
    if (!note) return;
    if (r && r.ok) {
      note.textContent = _lcT('local.browserPageOpened',
        '已在你的浏览器打开扩展管理页，路径已复制 —— 剩下三步只能你来点。');
    } else {
      note.textContent = _lcT('local.browserPageOpenFailed',
        '没能替你打开 —— 请自己打开浏览器的扩展管理页，路径已复制。');
    }
  });
}

/* Human-readable size for a download label (bytes → '115 MB'). */
function _lcFmtSize(bytes) {
  var n = Number(bytes);
  if (!isFinite(n) || n <= 0) return '';
  var mb = n / 1048576;
  return (mb >= 100 ? Math.round(mb) : Math.round(mb * 10) / 10) + ' MB';
}

/* Re-base a server-built same-origin URL onto the CURRENT proxy base path.
 *
 * `downloads[].url` is built by the backend from request.host_url — which
 * under a path-prefixed cloud-IDE proxy (…/proxy/15000/) is the origin
 * WITHOUT the prefix: the proxy strips the prefix before forwarding, so the
 * backend structurally cannot see it. Clicking such a link hits the
 * gateway's default route and returns "not found" without the request ever
 * reaching Tofu (the access log shows zero /desktop/download hits). Same
 * failure class as the paper PDF URL (pdf_viewer.js _resolvePaperPdfUrl):
 * strip back to the canonical /api/... tail and re-apply the LIVE base path
 * via apiUrl(). URLs with no /api/ marker (the releases-page escape hatch)
 * pass through untouched, and so does everything when apiUrl is absent. */
function _lcResolveDlUrl(url) {
  if (!url || typeof apiUrl !== 'function') return url;
  var i = url.indexOf('/api/');
  if (i < 0) return url;
  return apiUrl(url.slice(i));
}

/* The desktop-app installer row for the visitor's OS — authored ONCE, shown
 * wherever the pairing instruction is (floor included, once a payload lands).
 *
 * ── Why per-platform links instead of the releases page ──
 * `download_url` alone points at `…/releases/latest`, a page carrying FIVE
 * assets (two DMGs, an .exe, a .tar.gz, SHA256SUMS). Handing that to a user who
 * asked "how do I install this" makes them identify their own OS and CPU
 * architecture from a list of filenames. The backend already knows the OS from
 * the request, so `downloads` carries the installer(s) this visitor can
 * actually run, each with a direct-download URL.
 *
 * ── Why this may render TWO links, and why that is correct ──
 * On macOS the architecture is genuinely unknowable unless the browser tells
 * us: an Apple Silicon Mac reports "Intel Mac OS X" in its UA. When
 * `getHighEntropyValues` is unavailable (Safari, older browsers) the backend
 * returns BOTH DMGs, and each is labelled with its chip so the user can pick in
 * one glance. Guessing one would give roughly half of Mac users a download
 * that refuses to open — a silent dead end far worse than a two-item choice.
 *
 * Always keeps the releases-page link as a secondary "all downloads" escape
 * hatch: it is the only thing that still works for an unrecognised platform, a
 * release missing an asset, or an unreachable GitHub API.
 *
 * The version is on the button because the server can only serve what its
 * mirror holds: an installer older than the server (`older_than_server`) may
 * not boot or pair, and the user must see that before installing it. */
function _lcDownloadLinks(d) {
  var page = ((d && d.download_url) || '').trim();
  var picks = (d && Array.isArray(d.downloads)) ? d.downloads : [];
  var html = '';
  if (picks.length) {
    var older = null;
    html += '<p class="lc-dl-row">';
    for (var i = 0; i < picks.length; i++) {
      var p = picks[i] || {};
      if (!p.url) continue;
      if (p.older_than_server && !older) older = p;
      // The label names the CHIP, not just the OS — the whole point of the
      // two-DMG case is telling the user which one is theirs. Size goes in
      // the label: a 100+ MB installer with no size shown is a bad surprise.
      html += '<a class="lc-dl-link lc-dl-direct" href="' +
        _lcEsc(_lcResolveDlUrl(p.url)) +
        '" target="_blank" rel="noopener noreferrer" title="' +
        _lcEsc(p.filename || '') + '">' +
        _lcEsc(_lcT('local.desktopDownloadFor', '下载桌面版') + ' · ' +
               (p.label || p.arch || '') +
               (p.version ? ' · v' + p.version : '') +
               (p.size ? ' · ' + _lcFmtSize(p.size) : '')) + '</a>' +
        // Provenance: an artifact served by THIS server (not the public
        // GitHub network) is the fast/reliable path — say so, or the user
        // cannot tell why this link is preferable to the releases page.
        (p.hosted === 'server'
          ? '<span class="lc-dl-hosted">' +
            _lcEsc(_lcT('local.desktopHosted', '服务器直连')) + '</span>'
          : '');
    }
    html += '</p>';
    if (older) {
      html += '<p class="lc-substep lc-warn">' + _lcEsc((d.installers_refreshing
        ? _lcT('local.desktopOlderRefreshing',
          '⚠️ 这个安装包是 v{version}，比服务器（v{server}）旧，可能无法启动或连接。服务器正在获取新版，完成后这里会自动换成新链接。')
        : _lcT('local.desktopOlder',
          '⚠️ 这个安装包是 v{version}，比服务器（v{server}）旧，可能无法启动或连接。服务器暂时没取到新版，请稍后再打开这里，或从「查看全部下载」获取。'))
        .replace('{version}', older.version || '?')
        .replace('{server}', d.server_version || '?')) + '</p>';
    }
    if (picks.length > 1) {
      // Say WHY there are two, or the choice reads as a UI defect.
      html += '<p class="lc-substep">' + _lcEsc(_lcT('local.desktopArchAmbiguous',
        '浏览器没告诉我们这台 Mac 的芯片型号（Apple Silicon 也会自称 Intel）。' +
        'Apple 芯片（M1/M2/M3…）选 arm64，Intel 芯片选 x86_64；' +
        '在「关于本机」里可以看到。')) + '</p>';
    }
  }
  if (page) {
    html += '<p class="lc-substep"><a class="lc-dl-link" id="lcDesktopDownload" href="' +
      _lcEsc(page) + '" target="_blank" rel="noopener noreferrer">' +
      _lcEsc(picks.length
        ? _lcT('local.desktopDownloadAll', '查看全部下载 ↗')
        : _lcT('local.desktopDownload', '下载桌面版 ↗')) + '</a></p>';
  }
  return html;
}

// ══════════════════════════════════════════════════════
//  Desktop switch — flips the REAL wire flag (the browser has none)
// ══════════════════════════════════════════════════════

function toggleDesktopFromLocalModal() {
  var sw = document.getElementById('lcDesktopSwitch');
  if (sw && sw.disabled) return;   // no node — turning it on grants nothing
  if (typeof _applyDesktopUI === 'function') {
    _applyDesktopUI(!LocalControlShellState.desktopEnabled);
  }
  if (typeof captureActiveConversationSettings === 'function') captureActiveConversationSettings();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  _lcSetSwitch('lcDesktopSwitch', LocalControlShellState.desktopEnabled,
    _lcReach.desktop !== false);
  _lcUpdateBadge();
}
