// @ts-check
/* Generated lazy retained runtime: browser-extension-presenters. Do not edit directly. */
import { featureRegistry as runtimeScope } from '../feature-registry';
import { t } from '../i18n/index';
import { escapeHtml } from '../html-safety';

const Api = runtimeScope.Api;
if (!Api || typeof Api !== 'object') throw new Error('browser-extension-presenters runtime dependency is unavailable: Api');
const BASE_PATH = runtimeScope.BASE_PATH;
if (BASE_PATH === undefined) throw new Error('browser-extension-presenters runtime dependency is unavailable: BASE_PATH');
const _safeClipboardWrite = runtimeScope._safeClipboardWrite;
if (typeof _safeClipboardWrite !== 'function') throw new Error('browser-extension-presenters runtime dependency is unavailable: _safeClipboardWrite');
const apiUrl = runtimeScope.apiUrl;
if (typeof apiUrl !== 'function') throw new Error('browser-extension-presenters runtime dependency is unavailable: apiUrl');
/* ===== migrated source: browser-extension.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   browser-extension — install, upgrade and status of the Tofu browser
   extension (the browser-tabs capability).

   Entry points: openBrowserExtensionModal({browserUpgrade}) /
   closeBrowserExtensionModal, reached from Settings → Search & Web Access
   ("Install or upgrade extension", or an adapter's upgrade action).
   Dependencies: Api.browser (status, openExtensions), apiUrl/BASE_PATH for
   the preseeded ZIP, _safeClipboardWrite.

   Computer (desktop node) control used to share this dialog as "Local
   Control". Owner decision 2026-11-17: device control is not "local", so the
   computer-control UI was removed until the multi-device design gives it a
   home (docs/modules/remote_execution.md). Only the browser surface remains.

   ── The one rule this file exists to enforce ──
   The card shows exactly ONE next action, chosen by DETECTED state. Never a
   menu of every possible path, and never an instruction the user cannot act
   on from where they are.

   ── …and the FLOOR that rule stands on ──
   Detection UPGRADES an instruction that is already on screen rather than
   being the thing that puts one there. `_bePaintFloor` runs synchronously on
   open with the download instruction, which holds regardless of what the
   probe finds; the renderer replaces it with something MORE specific once the
   payload lands. A failed or missing status call therefore leaves a
   followable step, never an empty box or a permanent "checking".
   The floor and the detected `download` state are the SAME instruction, so
   they are ONE authoring (`_beBrowserDownload`).
   ═══════════════════════════════════════════════════════════════════ */

/* Poll cadence while the modal is OPEN, so a user who loads the extension
 * while looking at this dialog sees the dot flip without reopening it.
 * Cleared on close so a background tab never polls. */
var _BE_POLL_MS = 3000;
var _bePollTimer = null;

/* Settings can open this modal because a site adapter negotiated a concrete
 * browser capability gap even when a legacy extension reports no manifest
 * version; retain that stronger signal for this modal session. */
var _beUpgradeRequested = false;

function openBrowserExtensionModal(options) {
  var el = document.getElementById('browserExtensionModal');
  if (!el) return;
  _beUpgradeRequested = !!(options && options.browserUpgrade);
  el.classList.add('open');
  _bePaintFloor();
  _beRefresh();
  if (_bePollTimer) clearInterval(_bePollTimer);
  _bePollTimer = setInterval(_beRefresh, _BE_POLL_MS);
}

/* Status reads "not installed" rather than "checking": for a user who has
 * not set this up — the only user who needs this dialog — that is both the
 * honest answer and the one the poll is about to confirm. */
function _bePaintFloor() {
  _beSetStatus(false, _beT('browserExt.notInstalled', '尚未安装'));
  _beBrowserDownload();
}

function closeBrowserExtensionModal() {
  var el = document.getElementById('browserExtensionModal');
  if (el) el.classList.remove('open');
  _beUpgradeRequested = false;
  if (_bePollTimer) { clearInterval(_bePollTimer); _bePollTimer = null; }
}

function _beRefresh() {
  if (typeof Api === 'undefined' || !Api.browser) return;
  Promise.resolve(Api.browser.status())
    .then(_beRenderBrowser)
    .catch(function (e) { _beRenderBrowser(null, e); });
}

function _beT(key, fallback) {
  if (typeof t === 'function') {
    var v = t(key);
    if (v && v !== key) return v;
  }
  return fallback;
}

function _beEsc(s) {
  if (typeof escapeHtml === 'function') return escapeHtml(String(s == null ? '' : s));
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function _beSetStatus(connected, label) {
  var box = document.getElementById('beBrowserStatus');
  if (!box) return;
  var dot = box.querySelector('.browser-status-dot');
  if (dot) {
    dot.classList.toggle('connected', !!connected);
    dot.classList.toggle('disconnected', !connected);
  }
  var txt = box.querySelector('.be-status-text');
  if (txt) txt.textContent = label;
}

/* Which ONE instruction the card shows.
 *
 * BOTH inputs come from the backend's own detection, and BOTH are required
 * for the 'load_unpacked' branch:
 *
 *   - `extensionPath` — routes/api_v1/browser.py fills it only for a
 *     loopback peer whose machine also has a drivable browser.
 *   - `localBrowser` — the probe result: which Chromium-family browser this
 *     machine actually has, or null.
 *
 * The path's own gate is a pure IP test, which a same-host reverse proxy
 * makes vacuously true for public traffic; a path-only branch once shipped a
 * button that opened a browser window on a headless server. The probe is a
 * fact about the machine that no proxy can forge, so the conjunction stays
 * here as the frontend's own statement of the rule. */
function _beBrowserSetupState(d) {
  if (d && d.connected) return 'connected';
  if (d && d.extensionPath && d.localBrowser) return 'load_unpacked';
  return 'download';
}

/* Which connected client should automation go to? Prefer the extension that
 * stamped THIS document (origin_marker.js, data-tofu-browser-bridge) — with
 * two computers on one account, clients[0] is whatever polled first and can
 * be the OTHER machine's browser, which is how clicks land on a screen the
 * user is not looking at. Unknown/absent marker falls back to clients[0]. */
function _beLocalBrowserClientId(clients) {
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

function _beRenderBrowser(d, err) {
  var setup = document.getElementById('beBrowserSetup');
  var connected = !!(d && d.connected);

  if (err || !d) {
    _beSetStatus(false, _beT('browserExt.unreachable', '无法连接服务器'));
    // Losing the status call says nothing about whether the user needs the
    // extension; keep the one followable step on screen.
    _beBrowserDownload();
    return;
  }

  var clients = d.clients || [];
  /* Fleet recovery states: the server reports the version a fresh download
   * would carry (servedExtVersion) and the clients whose polls DIED at the
   * bridge gate (lockedOutClients) or strict protocol handshake
   * (incompatibleClients). A stale-but-connected extension still works — the
   * dot stays green and the upgrade is a one-click nudge. A locked-out
   * extension is installed, broken and unable to heal itself, so the card
   * says so and offers the preseeded re-download. */
  var servedVer = ((d && d.servedExtVersion) || '').trim();
  var staleFrom = '';
  if (connected && servedVer && clients.length) {
    /* Only THIS device's extension decides whether the nudge fires; a stale
     * side-load on another computer must not nag here. */
    var localId = _beLocalBrowserClientId(clients);
    var localVersion = '';
    for (var ci = 0; ci < clients.length; ci++) {
      if (clients[ci] && clients[ci].client_id === localId) {
        localVersion = ((clients[ci].ext_version) || '').trim();
        break;
      }
    }
    if (localVersion && localVersion !== servedVer) staleFrom = localVersion;
  }
  var lockedOut = (!connected && Array.isArray(d.lockedOutClients))
    ? d.lockedOutClients : [];
  var incompatible = (!connected && Array.isArray(d.incompatibleClients))
    ? d.incompatibleClients : [];
  if (connected) {
    var ago = (d.secondsAgo != null) ? d.secondsAgo + 's' : '';
    if (clients.length > 0) {
      runtimeScope._browserClientId = _beLocalBrowserClientId(clients);
    }
    /* Name WHICH extension binary is connected — the version is the only
     * at-a-glance evidence of a stale side-load. Only a fleet-wide agreement
     * earns the suffix; the upgrade nudge names a stale install. */
    var extVers = [];
    for (var vi = 0; vi < clients.length; vi++) {
      var vv = String((clients[vi] && clients[vi].ext_version) || '').trim();
      if (vv && extVers.indexOf(vv) === -1) extVers.push(vv);
    }
    var verSuffix = extVers.length === 1 ? ' · v' + extVers[0] : '';
    _beSetStatus(true,
      clients.length > 1
        ? _beT('browserExt.connectedN', '已连接 {n} 个').replace('{n}', clients.length) + verSuffix
        : _beT('browserExt.connected', '已连接') + verSuffix + (ago ? ' · ' + ago : ''));
  } else {
    runtimeScope._browserClientId = null;
    _beSetStatus(false,
      lockedOut.length
        ? _beT('browserExt.extDead', '已安装但凭证已失效')
        : incompatible.length
          ? _beT('browserExt.extUpgradeRequired', '已安装但需要升级')
          : _beT('browserExt.notInstalled', '尚未安装'));
  }

  // Chrome 142+ LNA guidance stays keyed on the CONNECTED extension's version.
  _applyBrowserLnaWarning(d.chromeMajor);

  if (!setup) return;
  var state = _beBrowserSetupState(d);
  if (state === 'connected') {
    if (staleFrom || _beUpgradeRequested) {
      // Outdated but WORKING: a nudge, not an alarm. A capability-triggered
      // entry has stronger evidence than a version diff: legacy extensions
      // often report no ext_version at all.
      var upgradeLead = staleFrom
        ? _beT('browserExt.outdated',
          '扩展有新版本（{old} → {new}）——重新下载 ZIP，覆盖解压到当初加载的那个文件夹即可：扩展发现新文件会自动重载完成升级（已自动配对，零配置）：')
          .replace('{old}', staleFrom).replace('{new}', servedVer)
        : _beT('browserExt.capabilityMismatch',
          '当前扩展已连接，但缺少站点适配器所需能力。重新下载 ZIP，覆盖解压到当初加载的文件夹，扩展会自动重载完成升级：');
      setup.innerHTML = '<p class="be-step">' + _beEsc(upgradeLead) + '</p>' +
        _beExtDownloadAction();
      _beWireExtDownload();
    } else {
      setup.innerHTML = '';
    }
    return;
  }

  if (lockedOut.length) {
    // Stranded: an installed extension is knocking with a dead credential.
    // Replacing it with the self-pairing build takes precedence over the
    // install guidance.
    setup.innerHTML =
      '<p class="be-step">' + _beEsc(_beT('browserExt.stranded',
        '检测到旧版扩展因凭证失效连不上（它自己无法恢复）——重新下载扩展 ZIP（已自动配对、零配置），覆盖解压到当初加载的文件夹，扩展自动重载即恢复：')) + '</p>' +
      _beExtDownloadAction();
    _beWireExtDownload();
    return;
  }

  if (incompatible.length) {
    // The owner/device authenticated, so re-pairing is the wrong cure: its
    // protocol is old. Name that exact state and offer the current binary.
    var rejected = incompatible[0] || {};
    var reportedProtocol = rejected.protocol_version || '?';
    var requiredProtocol = d.protocolVersion || '?';
    setup.innerHTML =
      '<p class="be-step">' + _beEsc(_beT('browserExt.protocolMismatch',
        '检测到浏览器扩展协议过旧（v{old} → v{new}），后端已安全拒绝命令。重新下载扩展 ZIP，覆盖解压到当初加载的文件夹，扩展自动重载即可恢复：')
        .replace('{old}', reportedProtocol).replace('{new}', requiredProtocol)) + '</p>' +
      _beExtDownloadAction();
    _beWireExtDownload();
    return;
  }

  if (state === 'load_unpacked') {
    // Tofu runs on this machine, this machine HAS a browser we can drive, and
    // the unpacked extension is already on disk. The remaining steps are
    // inside the browser's sandbox; the text says so. The browser is named
    // from the PROBE, never hardcoded.
    var lb = d.localBrowser || {};
    var bname = lb.name || 'Chrome';
    setup.innerHTML =
      '<button type="button" class="btn btn-primary btn-sm" id="beExtOpenBtn">' +
        _beEsc(_beT('browserExt.openPageBtn',
          '帮我打开扩展管理页（自动复制路径）')) + '</button>' +
      '<p class="be-step">' + _beEsc(
        _beT('browserExt.loadUnpacked',
          '剩下的三步 {browser} 不允许网页代劳：① 打开右上角「开发者模式」→ ② 点「加载已解压的扩展程序」→ ③ 粘贴路径（已自动复制）选择这个文件夹：')
        .replace('{browser}', bname)) + '</p>' +
      '<code class="be-copy" id="beExtPath" data-tooltip="' +
        _beEsc(_beT('browser.clickToCopy', '点击复制')) + '">' +
        _beEsc(d.extensionPath) + '</code>' +
      '<p class="be-step" id="beExtOpenNote"></p>';
    var openBtn = document.getElementById('beExtOpenBtn');
    if (openBtn) {
      openBtn.onclick = function () { _beOpenExtensionsPage(d.extensionPath); };
    }
    var code = document.getElementById('beExtPath');
    if (code) {
      code.onclick = function () {
        _safeClipboardWrite(d.extensionPath)
          .then(function () { code.classList.add('copied'); })
          .catch(function () {});
      };
    }
    return;
  }

  // Either the folder is not on the user's machine (remote server) or this
  // machine has no browser we could drive. Both reduce to the same ONE path:
  // download the ZIP, then load it in YOUR browser.
  _beBrowserDownload();
}

/* The download BUTTON — authored once, shared by the install instruction,
 * the upgrade nudge and the stranded-rescue branch. */
function _beExtDownloadAction() {
  return '<button type="button" class="btn btn-primary btn-sm" id="beExtDownloadBtn">' +
    _beEsc(_beT('browser.stepDownloadBtn', '下载扩展 ZIP')) + '</button>';
}

function _beWireExtDownload() {
  var btn = document.getElementById('beExtDownloadBtn');
  if (btn) btn.onclick = downloadBrowserExtension;
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
 * work. Only this card renders the recovery guidance. */
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
      _safeClipboardWrite(pol.textContent)
        .then(function () { pol.classList.add('copied'); })
        .catch(function () {});
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
    pathEl.style.display = '';
    pathEl.innerHTML = _beEsc(_beT('browser.lnaPathLabel', 'Place it at:')) +
      ' <code>' + _beEsc(dir) + '</code>';
  }
}

/* The download-the-ZIP instruction — the pre-detection floor, a failed
 * status call and the detected `download` state are the same instruction. */
function _beBrowserDownload() {
  var setup = document.getElementById('beBrowserSetup');
  if (!setup) return;
  setup.innerHTML =
    '<p class="be-step">' + _beEsc(_beT('browserExt.download',
      '下载扩展并解压，然后在 Chrome / Edge 里打开扩展管理页 → 开启「开发者模式」→「加载已解压的扩展程序」→ 选择解压出的文件夹。')) + '</p>' +
    _beExtDownloadAction();
  _beWireExtDownload();
}

/* The ONE action of the on-disk case: ask the server to open this machine's
 * browser at its extensions page, and copy the extension path from the
 * FRONTEND (a headless server has no clipboard). The three remaining clicks
 * live inside the browser's sandbox — the note never claims the install is
 * finished. */
function _beOpenExtensionsPage(path) {
  var btn = document.getElementById('beExtOpenBtn');
  var note = document.getElementById('beExtOpenNote');
  if (btn) btn.disabled = true;
  var copied = path
    ? Promise.resolve(_safeClipboardWrite(path)).catch(function () {})
    : Promise.resolve();
  var opened = (typeof Api.browser.openExtensions === 'function')
    ? Promise.resolve(Api.browser.openExtensions()).catch(function () { return null; })
    : Promise.resolve(null);
  Promise.all([copied, opened]).then(function (results) {
    if (btn) btn.disabled = false;
    var r = results[1];
    if (!note) return;
    note.textContent = (r && r.ok)
      ? _beT('browserExt.pageOpened',
        '已在你的浏览器打开扩展管理页，路径已复制 —— 剩下三步只能你来点。')
      : _beT('browserExt.pageOpenFailed',
        '没能替你打开 —— 请自己打开浏览器的扩展管理页，路径已复制。');
  });
}

// BEGIN GENERATED LAZY RUNTIME PORTS — browser-extension-presenters
// END GENERATED LAZY RUNTIME PORTS
// BEGIN GENERATED LAZY RUNTIME ACTIONS — browser-extension-presenters
runtimeScope.closeBrowserExtensionModal = closeBrowserExtensionModal;
runtimeScope.openBrowserExtensionModal = openBrowserExtensionModal;
// END GENERATED LAZY RUNTIME ACTIONS
