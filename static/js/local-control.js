/* ═══════════════════════════════════════════════════════════════════
   local-control — the SINGLE "let Tofu act on my machine" surface.

   Merges what used to be two toolbar rows (#browserToggle + #desktopToggle),
   one setup modal (#browserModal) and one blind flag flip (toggleDesktop with
   no status check at all). From the user's side browser-tabs and
   computer-control are one concept; two rows, two modals and two status dots
   were strictly more cognitive load than one.

   What did NOT merge, deliberately: the two backing flags `browserEnabled`
   and `desktopEnabled` stay separate on the wire. They gate different tool
   families with genuinely different risk tiers (reading a tab vs running a
   shell command), and `lib/tools/registry/_build.py` builds them from two
   independent ToolContext fields. Only the SURFACE is merged.

   ── The one rule this file exists to enforce ──
   Each capability row shows exactly ONE next action, chosen by DETECTED
   state. Never a menu of every possible path, and never an instruction the
   user cannot act on from where they are. The desktop choice is made by the
   BACKEND (`setup_state` on /api/v1/desktop/status) because only the server
   process can see `sys.frozen` — the frontend must not re-derive it.

   This file is concatenated by lib/js_bundler.py — symbols share the same
   window scope as every other static/js/*.js file. No imports/exports.
   ═══════════════════════════════════════════════════════════════════ */

/* Poll cadence while the modal is OPEN. `is_desktop_agent_connected()` is a
 * 15s window (lib/desktop/bridge.py::_CONNECTED_WINDOW_S) and enabling the
 * tray agent takes a couple of seconds, so a user who turns it on WHILE
 * looking at this dialog must see the dot flip without reopening it. The old
 * _checkBrowserStatus was one-shot-on-open; that limitation is not carried
 * over. Cleared on close so a background tab never polls. */
var _LC_POLL_MS = 3000;
var _lcPollTimer = null;

function openLocalControlModal() {
  var el = document.getElementById('localControlModal');
  if (!el) return;
  el.classList.add('open');
  _lcRefresh();
  if (_lcPollTimer) clearInterval(_lcPollTimer);
  _lcPollTimer = setInterval(_lcRefresh, _LC_POLL_MS);
}

function closeLocalControlModal() {
  var el = document.getElementById('localControlModal');
  if (el) el.classList.remove('open');
  if (_lcPollTimer) { clearInterval(_lcPollTimer); _lcPollTimer = null; }
}

/* Fetch both capabilities' state and repaint. Each side is independent —
 * one backend hiccup must not blank the other row. */
function _lcRefresh() {
  if (typeof Api === 'undefined' || !Api.browser || !Api.desktop) return;
  Promise.resolve(Api.browser.status())
    .then(_lcRenderBrowser)
    .catch(function (e) { _lcRenderBrowser(null, e); });
  Promise.resolve(Api.desktop.status())
    .then(_lcRenderDesktop)
    .catch(function (e) { _lcRenderDesktop(null, e); });
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

/* Paint one row's switch (reflects the real wire flag, not modal-local state). */
function _lcSetSwitch(switchId, on) {
  var sw = document.getElementById(switchId);
  if (!sw) return;
  sw.classList.toggle('on', !!on);
  sw.setAttribute('aria-checked', on ? 'true' : 'false');
}

// ══════════════════════════════════════════════════════
//  Browser tabs
// ══════════════════════════════════════════════════════

/* Which ONE instruction the browser row shows.
 *
 * `extensionPath` is already the backend's own detected answer:
 * routes/api_v1/browser.py only fills it for a loopback peer, precisely
 * because a remote user's Chrome cannot load a server-side folder. So the
 * branch here consumes that decision rather than re-deriving it. */
function _lcBrowserSetupState(d) {
  if (d && d.connected) return 'connected';
  if (d && d.extensionPath) return 'load_unpacked';
  return 'download';
}

function _lcRenderBrowser(d, err) {
  var setup = document.getElementById('lcBrowserSetup');
  var connected = !!(d && d.connected);

  if (err || !d) {
    _lcSetStatus('lcBrowserStatus', false, _lcT('local.unreachable', '无法连接服务器'));
    if (setup) setup.innerHTML = '';
    return;
  }

  var clients = d.clients || [];
  if (connected) {
    var ago = (d.secondsAgo != null) ? d.secondsAgo + 's' : '';
    if (clients.length > 0) {
      window._browserClientId = clients[0].client_id;
    }
    _lcSetStatus('lcBrowserStatus', true,
      clients.length > 1
        ? _lcT('local.connectedN', '已连接').replace('{n}', clients.length)
        : _lcT('local.connected', '已连接') + (ago ? ' · ' + ago : ''));
  } else {
    window._browserClientId = null;
    _lcSetStatus('lcBrowserStatus', false, _lcT('local.notInstalled', '尚未安装'));
  }

  _lcSetSwitch('lcBrowserSwitch',
    typeof browserEnabled !== 'undefined' && browserEnabled);

  // Chrome 142+ LNA guidance stays keyed on the CONNECTED extension's version.
  if (typeof _applyBrowserLnaWarning === 'function') {
    _applyBrowserLnaWarning(d.chromeMajor);
  }

  if (!setup) return;
  var state = _lcBrowserSetupState(d);
  if (state === 'connected') { setup.innerHTML = ''; return; }

  if (state === 'load_unpacked') {
    // Tofu runs on this machine — the unpacked extension is already on disk.
    // One action: load that folder. No download, no unzip.
    setup.innerHTML =
      '<p class="lc-step">' + _lcEsc(_lcT('local.browserLoadUnpacked',
        '打开 chrome://extensions/ → 打开右上角「开发者模式」→ 点「加载已解压的扩展程序」→ 选择下面这个文件夹：')) + '</p>' +
      '<code class="lc-copy" id="lcExtPath" data-tooltip="' +
        _lcEsc(_lcT('browser.clickToCopy', '点击复制')) + '">' +
        _lcEsc(d.extensionPath) + '</code>';
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

  // Remote server: the folder does not exist on the user's machine, so the
  // only actionable path is download-then-load.
  setup.innerHTML =
    '<p class="lc-step">' + _lcEsc(_lcT('local.browserDownload',
      '下载扩展并解压，然后在 chrome://extensions/ 打开「开发者模式」→「加载已解压的扩展程序」→ 选择解压出的文件夹。')) + '</p>' +
    '<button type="button" class="btn btn-primary btn-sm" id="lcExtDownloadBtn">' +
      _lcEsc(_lcT('browser.stepDownloadBtn', '下载扩展 ZIP')) + '</button>';
  var btn = document.getElementById('lcExtDownloadBtn');
  if (btn) {
    btn.onclick = function () {
      if (typeof downloadBrowserExtension === 'function') downloadBrowserExtension();
    };
  }
}

// ══════════════════════════════════════════════════════
//  This computer
// ══════════════════════════════════════════════════════

function _lcRenderDesktop(d, err) {
  var setup = document.getElementById('lcDesktopSetup');
  if (err || !d) {
    _lcSetStatus('lcDesktopStatus', false, _lcT('local.unreachable', '无法连接服务器'));
    if (setup) setup.innerHTML = '';
    return;
  }

  var connected = !!d.connected;
  _lcSetStatus('lcDesktopStatus', connected,
    connected ? _lcT('local.connected', '已连接')
              : _lcT('local.notRunning', '未运行'));
  _lcSetSwitch('lcDesktopSwitch',
    typeof desktopEnabled !== 'undefined' && desktopEnabled);

  if (!setup) return;

  // The backend chose the state — see routes/api_v1/desktop.py::_setup_state.
  // Reading it (rather than re-deriving from the URL) is what keeps the
  // packaged-app case distinguishable from a reverse-proxied remote one.
  switch (d.setup_state) {
    case 'connected':
      setup.innerHTML = '';
      return;

    case 'tray':
      // Packaged desktop app: the agent runs IN-PROCESS. One click, no token,
      // no second program to install.
      setup.innerHTML = '<p class="lc-step">' + _lcEsc(_lcT('local.desktopTray',
        '右键点击系统托盘里的 Tofu 图标 → 勾选「Enable Computer Control」。')) + '</p>';
      return;

    case 'local_source':
      // Tofu is running from source on this same machine. Pointing the user at
      // "download the desktop app" would tell them to install a second copy of
      // something they are already running.
      setup.innerHTML = '<p class="lc-step">' + _lcEsc(_lcT('local.desktopSource',
        '当前 Tofu 以源码方式运行。安装桌面版后即可在系统托盘一键开启「Enable Computer Control」。')) + '</p>';
      return;

    default:
      // Remote server — the ONLY case that needs a token, so it is the only
      // case that shows one.
      setup.innerHTML =
        '<p class="lc-step">' + _lcEsc(_lcT('local.desktopRemote',
          'Tofu 运行在远程服务器上。在你自己的电脑安装桌面版，然后用下面这个令牌把它连过来：')) + '</p>' +
        '<button type="button" class="btn btn-primary btn-sm" id="lcMintBtn">' +
          _lcEsc(_lcT('local.mintToken', '生成连接令牌')) + '</button>' +
        '<code class="lc-copy" id="lcTokenBox" style="display:none"></code>';
      var mint = document.getElementById('lcMintBtn');
      if (mint) mint.onclick = _lcMintToken;
      return;
  }
}

/* Mint a bridge token inline. Reuses the endpoint the Devices settings page
 * already drives (POST /api/v1/desktop/token) — the raw secret is returned
 * exactly once, so it is shown here and never re-fetched. */
function _lcMintToken() {
  var btn = document.getElementById('lcMintBtn');
  var box = document.getElementById('lcTokenBox');
  if (btn) btn.disabled = true;
  Promise.resolve(Api.desktop.mintToken('local-control'))
    .then(function (r) {
      if (btn) btn.disabled = false;
      if (!r || !r.token) {
        if (typeof showToast === 'function') showToast(_lcT('devices.mintFailed', '生成失败'));
        return;
      }
      if (!box) return;
      box.style.display = '';
      box.textContent = r.token;
      box.setAttribute('data-tooltip', _lcT('browser.clickToCopy', '点击复制'));
      box.onclick = function () {
        if (typeof _safeClipboardWrite === 'function') {
          _safeClipboardWrite(r.token)
            .then(function () { box.classList.add('copied'); })
            .catch(function () {});
        }
      };
      if (btn) btn.style.display = 'none';
    })
    .catch(function () {
      if (btn) btn.disabled = false;
      if (typeof showToast === 'function') showToast(_lcT('devices.mintFailed', '生成失败'));
    });
}

// ══════════════════════════════════════════════════════
//  Switches — flip the REAL wire flags, one per capability
// ══════════════════════════════════════════════════════

function toggleBrowserFromLocalModal() {
  if (typeof _applyBrowserUI === 'function') _applyBrowserUI(!browserEnabled);
  if (typeof _saveConvToolState === 'function') _saveConvToolState();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  _lcSetSwitch('lcBrowserSwitch', browserEnabled);
  _lcUpdateBadge();
}

function toggleDesktopFromLocalModal() {
  if (typeof _applyDesktopUI === 'function') _applyDesktopUI(!desktopEnabled);
  if (typeof _saveConvToolState === 'function') _saveConvToolState();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  _lcSetSwitch('lcDesktopSwitch', desktopEnabled);
  _lcUpdateBadge();
}

/* ONE summary badge on the merged toolbar entry, counting whichever
 * capabilities are on. The merged row is `active` when either is. */
function _lcUpdateBadge() {
  var n = ((typeof browserEnabled !== 'undefined' && browserEnabled) ? 1 : 0)
        + ((typeof desktopEnabled !== 'undefined' && desktopEnabled) ? 1 : 0);
  var badge = document.getElementById('localControlBadge');
  if (badge) {
    badge.textContent = n > 0 ? String(n) : '';
    badge.style.display = n > 0 ? '' : 'none';
    badge.classList.toggle('visible', n > 0);
  }
  var row = document.getElementById('localControlToggle');
  if (row) row.classList.toggle('active', n > 0);
}

if (typeof window !== 'undefined') {
  window.openLocalControlModal = openLocalControlModal;
  window.closeLocalControlModal = closeLocalControlModal;
  window.toggleBrowserFromLocalModal = toggleBrowserFromLocalModal;
  window.toggleDesktopFromLocalModal = toggleDesktopFromLocalModal;
  window._lcUpdateBadge = _lcUpdateBadge;
  window._lcBrowserSetupState = _lcBrowserSetupState;
  window._lcRenderBrowser = _lcRenderBrowser;
  window._lcRenderDesktop = _lcRenderDesktop;
}
