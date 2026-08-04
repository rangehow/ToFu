"""Single theme source for every Tofu tkinter dialog.

Both native dialogs (first-launch component picker, remote-connect prompt)
used to be stock gray ttk with hardcoded English strings and ad-hoc colors
(``foreground='gray'`` / ``'#666'`` / ``'#b00'``) — a different universe
from the web UI's design-token system sitting one process away. This module
is the ONE place that owns:

* **Palettes** — LIGHT and DARK, derived from the web UI's own ``:root``
  tokens (static/styles.css), so the native chrome and the web UI read as
  one product. Identical key sets: a token added to one palette and not
  the other fails the palette parity test instead of KeyErroring a dialog.
* **Dark-mode detection** — TOFU_THEME override → Windows registry →
  macOS ``defaults`` → Linux ``gsettings``; every failure shape falls back
  to LIGHT (a wrong-light dialog is readable; a crashed one is not).
* **Bilingual strings** — zh/en by TOFU_LANG → OS locale → env; every key
  carries both languages (the web UI is fully bilingual; native chrome was
  the only English-only surface left).
* **ttk style application** — clam base + named ``Tofu.*`` styles, plus
  logo loading and DPI awareness (3-level fallback, moved here from the
  two private copies that had already diverged).

Rules for consumers: never hardcode a color, a font size family, or a
user-facing string in a dialog — add a token/key HERE and reference it.

Headless rule: tkinter is imported LAZILY inside functions (never at module
level) so tests can import this module on a display-less CI runner.
"""

import locale
import os
import subprocess
import sys

# ═══════════════════════════════════════════════════════════════
#  Palettes — from static/styles.css :root / [data-theme="light"]
# ═══════════════════════════════════════════════════════════════

DARK = {
    'bg': '#0a0a0c',
    'bg2': '#111115',
    'bg3': '#1a1a21',
    'hover': '#22222b',
    'active': '#2a2a35',
    'border': '#2a2a35',
    'border_light': '#33333f',
    'text': '#e8e8ed',
    'text2': '#9898a8',
    'text3': '#6a6a7a',
    'accent': '#6e56cf',
    'accent_hover': '#7c66d4',
    'accent_fg': '#ffffff',
    'success': '#10b981',
    'error': '#cf5050',
}

LIGHT = {
    'bg': '#f4f2ed',
    'bg2': '#eceae4',
    'bg3': '#e3e1da',
    'hover': '#d9d7cf',
    'active': '#d0cdc4',
    'border': '#c2beb3',
    'border_light': '#ada89c',
    'text': '#272732',
    'text2': '#504f5b',
    'text3': '#868490',
    'accent': '#6366f1',
    'accent_hover': '#4f46e5',
    'accent_fg': '#ffffff',
    'success': '#15803d',
    'error': '#b82020',
}


def detect_dark() -> bool:
    """True when the OS is in dark mode. Light fallback on ANY failure.

    Order: explicit TOFU_THEME override (wins over the OS probe, and costs
    no subprocess), then the per-platform probe. Never raises.
    """
    override = (os.environ.get('TOFU_THEME') or '').strip().lower()
    if override in ('dark', 'light'):
        return override == 'dark'
    try:
        if sys.platform.startswith('win'):
            # HKCU\...\Themes\Personalize:AppsUseLightTheme — 0 means dark.
            import winreg  # noqa: PLC0415 — Windows-only module, lazy by design
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
            try:
                value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
            finally:
                winreg.CloseKey(key)
            return value == 0
        if sys.platform == 'darwin':
            # The key EXISTS only in dark mode — exit 1 means light.
            r = subprocess.run(
                ['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                capture_output=True, text=True, timeout=2)
            return r.returncode == 0 and 'dark' in (r.stdout or '').lower()
        if sys.platform.startswith('linux'):
            # GNOME 42+ color-scheme; absent gsettings/old GNOME → light.
            r = subprocess.run(
                ['gsettings', 'get', 'org.gnome.desktop.interface',
                 'color-scheme'],
                capture_output=True, text=True, timeout=2)
            return 'dark' in (r.stdout or '').lower()
    except Exception:
        return False
    return False


def current_palette() -> dict:
    """The DARK or LIGHT token dict for the current OS state."""
    return DARK if detect_dark() else LIGHT


def detect_lang() -> str:
    """'zh' or 'en'. TOFU_LANG wins, then the OS locale, then LANG-family
    env vars; 'en' when nothing says otherwise. Never raises."""
    override = (os.environ.get('TOFU_LANG') or '').strip().lower()
    if override.startswith('zh'):
        return 'zh'
    if override.startswith('en'):
        return 'en'
    try:
        loc = locale.getlocale()[0] or ''
    except Exception:
        loc = ''
    if loc.lower().startswith('zh'):
        return 'zh'
    if loc:
        return 'en'
    for var in ('LC_ALL', 'LC_MESSAGES', 'LANG'):
        v = (os.environ.get(var) or '').strip().lower()
        if v:
            return 'zh' if v.startswith('zh') else 'en'
    return 'en'


# ═══════════════════════════════════════════════════════════════
#  Strings — every user-facing native-chrome string, both languages
# ═══════════════════════════════════════════════════════════════

STRINGS = {
    'desktop.components.title': {
        'en': 'Optional Components',
        'zh': '可选组件',
    },
    'desktop.components.subtitle': {
        'en': 'These components can be downloaded now or later. '
              'Recommended components are pre-selected.',
        'zh': '以下组件可以现在下载，也可以稍后通过系统托盘安装。推荐组件已默认勾选。',
    },
    'desktop.components.install': {
        'en': 'Install Selected',
        'zh': '安装所选',
    },
    'desktop.components.skip': {
        'en': 'Skip for now',
        'zh': '暂不安装',
    },
    'desktop.components.close': {
        'en': 'Close',
        'zh': '关闭',
    },
    'desktop.components.pending': {
        'en': 'Pending',
        'zh': '等待中',
    },
    'desktop.components.installing': {
        'en': 'Installing…',
        'zh': '安装中…',
    },
    'desktop.components.installed': {
        'en': 'Installed',
        'zh': '已安装',
    },
    'desktop.components.alreadyInstalled': {
        'en': 'Already installed.',
        'zh': '已安装。',
    },
    'desktop.components.failed': {
        'en': 'Failed',
        'zh': '失败',
    },
    'desktop.components.summaryOk': {
        'en': 'All selected components are installed.',
        'zh': '所选组件已全部安装完成。',
    },
    'desktop.components.summaryFail': {
        'en': '{n} component(s) failed — details above. '
              'You can retry from the tray menu.',
        'zh': '有 {n} 个组件安装失败，详见上方信息。可稍后从系统托盘菜单重试。',
    },
    'desktop.connect.title': {
        'en': 'Connect to remote Tofu',
        'zh': '连接到远程 Tofu',
    },
    'desktop.connect.heading': {
        'en': 'Connect this computer to a remote Tofu',
        'zh': '将这台电脑连接到远程 Tofu',
    },
    'desktop.connect.instructions': {
        'en': 'In Tofu, open Local Control → This computer and press '
              '"Pair this computer" — the 6-digit code needs no address. '
              'This line is the advanced fallback: paste it whole.',
        'zh': '在 Tofu 中打开「本机控制 → 这台电脑」，优先点「配对这台电脑」'
              '用 6 位码配对（无需地址）。连接行是高级兜底：将整行粘贴到这里。',
    },
    'desktop.connect.current': {
        'en': 'Currently attached to: {url}',
        'zh': '当前已连接到：{url}',
    },
    'desktop.connect.cancel': {
        'en': 'Cancel',
        'zh': '取消',
    },
    'desktop.connect.connect': {
        'en': 'Connect',
        'zh': '连接',
    },
    'desktop.connect.verifying': {
        'en': 'Verifying the server address…',
        'zh': '正在验证服务器地址…',
    },
    'desktop.connect.verifyFailed': {
        'en': 'Cannot reach Tofu there: {reason}. An agent cannot use a '
              'proxy/SSO address — prefer the pairing code (the agent '
              'discovers the route itself). Press Connect again to save '
              'anyway.',
        'zh': '连不上服务器：{reason}。代理/SSO 地址受控端用不了——建议改用'
              '配对码（通路由受控端自己发现）；再点一次「连接」强制保存。',
    },
    'desktop.pair.title': {
        'en': 'Pair this computer',
        'zh': '配对这台电脑',
    },
    'desktop.pair.heading': {
        'en': 'Pair this computer with Tofu',
        'zh': '将这台电脑与 Tofu 配对',
    },
    'desktop.pair.instructions': {
        'en': 'In Tofu, open Local Control → This computer and press '
              '"Pair this computer", then type the 6-digit code below. '
              'The server address is pre-filled — edit it only if it is '
              'wrong.',
        'zh': '在 Tofu 面板「本机控制 → 这台电脑」点「配对这台电脑」，'
              '把 6 位配对码填到下面。服务器地址已自动填好，不对再改。',
    },
    'desktop.pair.serverLabel': {
        'en': 'Server address',
        'zh': '服务器地址',
    },
    'desktop.pair.codeLabel': {
        'en': 'Pairing code',
        'zh': '配对码',
    },
    'desktop.pair.connect': {
        'en': 'Pair and connect',
        'zh': '配对并连接',
    },
    'desktop.pair.useLine': {
        'en': 'Use a connect line instead…',
        'zh': '改用连接行…',
    },
    'desktop.pair.cancel': {
        'en': 'Cancel',
        'zh': '取消',
    },
    'desktop.pair.verifying': {
        'en': 'Pairing…',
        'zh': '正在配对…',
    },
    'desktop.pair.badAddress': {
        'en': 'The server address must start with http:// or https://.',
        'zh': '服务器地址必须以 http:// 或 https:// 开头。',
    },
    'desktop.pair.badCode': {
        'en': 'The pairing code is the 6-digit number shown in the panel.',
        'zh': '配对码是面板生成的 6 位数字。',
    },
    'desktop.pair.invalidCode': {
        'en': 'This code is invalid, expired or already used — mint a '
              'fresh one in the panel.',
        'zh': '配对码无效、过期或已被使用——请回面板重新生成。',
    },
    'desktop.pair.rateLimited': {
        'en': 'Too many failed attempts from this address. Wait a few '
              'minutes and try again with a fresh code.',
        'zh': '此地址失败次数过多——请等几分钟，用新配对码重试。',
    },
    'desktop.pair.failed': {
        'en': 'Could not pair at that address: {reason}. Check the '
              'server address (a proxy/SSO gateway cannot be used).',
        'zh': '无法在该地址完成配对：{reason}。请检查服务器地址'
              '（代理/SSO 地址不可用）。',
    },
    'desktop.comp.postgresql.name': {
        'en': 'PostgreSQL Database',
        'zh': 'PostgreSQL 数据库',
    },
    'desktop.comp.postgresql.desc': {
        'en': 'High-performance database: better concurrency, JSONB, '
              'full-text search. Without it the app uses SQLite '
              '(single-user, still fully functional).',
        'zh': '高性能数据库：更好的并发、JSONB 与全文检索。'
              '不安装则使用 SQLite（单用户场景同样完整可用）。',
    },
    'desktop.comp.chromium.name': {
        'en': 'Browser Engine (Chromium)',
        'zh': '浏览器引擎（Chromium）',
    },
    'desktop.comp.chromium.desc': {
        'en': 'Enables fetching JavaScript-rendered pages and browser '
              'automation. Required for fetch_url on JS-heavy sites.',
        'zh': '支持抓取需要 JavaScript 渲染的网页与浏览器自动化，'
              '是 fetch_url 应对重 JS 站点的前提。',
    },
    # ── System-tray strings (pystray menus of BOTH launchers) ──
    # The tray was the last English-only surface: every MenuItem literal was
    # hardcoded. tests/test_desktop_tray_i18n.py AST-ratchets both launchers
    # so a literal can never come back. Placeholder tokens ({tag}/{url}/
    # {port}) are filled by the call sites via .replace().
    'desktop.tray.open': {
        'en': 'Open Tofu',
        'zh': '打开 Tofu',
    },
    'desktop.tray.downloadUpdate': {
        'en': 'Download update ({tag})',
        'zh': '下载更新（{tag}）',
    },
    'desktop.tray.enableCC': {
        'en': 'Enable Computer Control',
        'zh': '启用电脑控制',
    },
    'desktop.tray.permissions': {
        'en': 'Permissions',
        'zh': '权限',
    },
    'desktop.tray.permWrite': {
        'en': 'Allow file writes',
        'zh': '允许写入文件',
    },
    'desktop.tray.permExec': {
        'en': 'Allow run commands / open apps',
        'zh': '允许运行命令 / 打开应用',
    },
    'desktop.tray.permGui': {
        'en': 'Allow mouse / keyboard / screenshot',
        'zh': '允许鼠标 / 键盘 / 截图',
    },
    'desktop.tray.permEgress': {
        'en': 'Allow relaying subscription API traffic',
        'zh': '允许转发订阅 API 流量',
    },
    'desktop.tray.connectRemote': {
        'en': 'Connect to remote Tofu…',
        'zh': '连接到远程 Tofu…',
    },
    'desktop.tray.connectDifferent': {
        'en': 'Connect to a different Tofu…',
        'zh': '连接到另一个 Tofu…',
    },
    'desktop.tray.installComponents': {
        'en': 'Install Components...',
        'zh': '安装组件...',
    },
    'desktop.tray.serverLabel': {
        'en': 'Server: {url}',
        'zh': '服务器：{url}',
    },
    'desktop.tray.serverLocal': {
        'en': 'this computer (port {port})',
        'zh': '本机（端口 {port}）',
    },
    'desktop.tray.notAttached': {
        'en': '(not attached)',
        'zh': '（未连接）',
    },
    'desktop.tray.autostart': {
        'en': 'Start with Windows',
        'zh': '开机自启',
    },
    'desktop.tray.quit': {
        'en': 'Quit',
        'zh': '退出',
    },
    'desktop.tray.controlPanel': {
        'en': 'Control panel…',
        'zh': '控制面板…',
    },
    'desktop.tray.linkState': {
        'en': 'Link: {status}',
        'zh': '链路：{status}',
    },
    'desktop.tray.stOk': {
        'en': 'connected',
        'zh': '已连上',
    },
    'desktop.tray.stAuth': {
        'en': 'auth failed — the token in the connect line is wrong',
        'zh': '鉴权失败——连接行里的密钥不对',
    },
    'desktop.tray.stProxy': {
        'en': 'blocked by a proxy/SSO gateway — re-discovering the route '
              '(auto-tunnel included)',
        'zh': '地址被代理/SSO 拦截——正在自动重找通路（含自动隧道）',
    },
    'desktop.tray.stUnreachable': {
        'en': 'server unreachable — retrying and re-discovering the route '
              'by itself',
        'zh': '连不上服务器——正在自动重试并重找通路',
    },
    'desktop.tray.stHttp': {
        'en': 'server answered HTTP {code}',
        'zh': '服务器返回 HTTP {code}',
    },
    'desktop.tray.stStarting': {
        'en': 'connecting…',
        'zh': '连接中…',
    },
    # ── Startup role window (desktop/role_window.py) ──
    # Both launchers show this window at startup so the machine's ROLE is
    # never implicit again; it doubles as the control panel that used to
    # be tray-only.
    'desktop.role.serverTitle': {
        'en': 'This computer runs your Tofu server',
        'zh': '这台电脑是 Tofu 服务器',
    },
    'desktop.role.serverSub': {
        'en': 'Tofu is running locally. The web app opens in your '
              'browser automatically.',
        'zh': 'Tofu 正在本机运行，网页版会自动在浏览器中打开。',
    },
    'desktop.role.alsoClient': {
        'en': 'Also controlled by: {url}',
        'zh': '同时受控于：{url}',
    },
    'desktop.role.agentTitle': {
        'en': 'This computer is controlled by a Tofu server',
        'zh': '这台电脑是 Tofu 受控端',
    },
    'desktop.role.agentSub': {
        'en': 'It answers to the server below. You can change '
              'permissions or re-attach at any time.',
        'zh': '它听命于下方服务器，你可以随时修改权限或重新连接。',
    },
    'desktop.role.ccTitle': {
        'en': 'Computer control',
        'zh': '电脑控制',
    },
    'desktop.role.ccOn': {
        'en': 'Enabled — remote control is ON',
        'zh': '已启用——远程控制开启中',
    },
    'desktop.role.ccOff': {
        'en': 'Disabled — this computer cannot be controlled',
        'zh': '未启用——这台电脑不可被控制',
    },
    'desktop.role.enable': {
        'en': 'Enable',
        'zh': '启用',
    },
    'desktop.role.disable': {
        'en': 'Disable',
        'zh': '停用',
    },
    'desktop.role.permHint': {
        'en': 'Permission tiers apply on the next command poll.',
        'zh': '权限档位在下一次指令轮询时生效。',
    },
    'desktop.role.showAtStartup': {
        'en': 'Show this window at startup',
        'zh': '启动时显示此窗口',
    },
    'desktop.role.minimize': {
        'en': 'Minimize to tray',
        'zh': '最小化到托盘',
    },
}


def t(key: str, lang: str = None) -> str:
    """Look up a string in the current (or given) language.

    Missing key → the key itself (visible in dev, never a crash); missing
    translation → English.
    """
    lang = lang or detect_lang()
    pair = STRINGS.get(key)
    if not pair:
        return key
    return pair.get(lang) or pair.get('en') or key


# ═══════════════════════════════════════════════════════════════
#  tk application — LAZY imports (headless CI must import this file)
# ═══════════════════════════════════════════════════════════════

def ensure_dpi_awareness(log=lambda msg: None) -> None:
    """Mark the process per-monitor DPI-aware so HiDPI rendering is crisp.

    THE 3-level fallback (Per-Monitor v2 → Per-Monitor → System), moved here
    from launcher.py / post_install.py whose two private copies had already
    diverged (the post_install one lacked the v2 level). Must run before any
    window is created. No-op off Windows. Never raises.
    """
    if not sys.platform.startswith('win'):
        return
    try:
        import ctypes
    except Exception as e:  # pragma: no cover
        log('DPI awareness unavailable: %s' % e)
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4 (Win 10 1703+)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Win 8.1+)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception as e:
        log('Could not set DPI awareness: %s' % e)


def apply_theme(root, palette=None) -> dict:
    """Theme a tk root: background + clam-based ``Tofu.*`` ttk styles.

    Returns the palette in use so the dialog can color non-ttk widgets
    (tk Frames used as cards, the window itself).
    """
    import tkinter as tk  # noqa: PLC0415 — lazy, headless rule
    from tkinter import ttk

    p = palette or current_palette()
    root.configure(bg=p['bg'])

    style = ttk.Style(root)
    try:
        style.theme_use('clam')
    except tk.TclError:
        pass  # clam is shipped with every std tk; belt-and-braces only

    base_font = ('', 10)
    style.configure('Tofu.TFrame', background=p['bg'])
    style.configure('Card.TFrame', background=p['bg2'])
    style.configure('Tofu.TLabel', background=p['bg'], foreground=p['text'],
                    font=base_font)
    style.configure('Tofu.Title.TLabel', background=p['bg'],
                    foreground=p['text'], font=('', 15, 'bold'))
    style.configure('Tofu.Sub.TLabel', background=p['bg'],
                    foreground=p['text2'], font=('', 10))
    style.configure('Tofu.Err.TLabel', background=p['bg'],
                    foreground=p['error'], font=('', 9))
    style.configure('Card.TLabel', background=p['bg2'], foreground=p['text'],
                    font=base_font)
    style.configure('CardName.TLabel', background=p['bg2'],
                    foreground=p['text'], font=('', 10, 'bold'))
    style.configure('CardSub.TLabel', background=p['bg2'],
                    foreground=p['text2'], font=('', 9))
    style.configure('Status.Ok.TLabel', background=p['bg2'],
                    foreground=p['success'], font=('', 9))
    style.configure('Status.Err.TLabel', background=p['bg2'],
                    foreground=p['error'], font=('', 9))
    style.configure('Tofu.TCheckbutton', background=p['bg2'],
                    foreground=p['text'], font=base_font)
    style.map('Tofu.TCheckbutton',
              background=[('active', p['bg2'])],
              foreground=[('disabled', p['text3'])])
    style.configure('Tofu.TButton', padding=(14, 7), background=p['bg3'],
                    foreground=p['text'], borderwidth=1, font=base_font)
    style.map('Tofu.TButton',
              background=[('active', p['hover']), ('disabled', p['bg3'])],
              foreground=[('disabled', p['text3'])])
    style.configure('Tofu.Accent.TButton', padding=(14, 7),
                    background=p['accent'], foreground=p['accent_fg'],
                    borderwidth=0, font=('', 10, 'bold'))
    style.map('Tofu.Accent.TButton',
              background=[('active', p['accent_hover']),
                          ('disabled', p['bg3'])],
              foreground=[('disabled', p['text3'])])
    style.configure('Tofu.Horizontal.TProgressbar', background=p['accent'],
                    troughcolor=p['bg3'], bordercolor=p['border'],
                    lightcolor=p['accent'], darkcolor=p['accent'])
    style.configure('Tofu.TEntry', fieldbackground=p['bg3'],
                    foreground=p['text'], insertcolor=p['text'],
                    bordercolor=p['border'])
    return p


def load_logo_photo(root, size=56):
    """Load the brand logo as a tk PhotoImage resized to ``size`` px.

    Returns None (never raises) when PIL or the asset is unavailable — a
    dialog without a logo is still a dialog. The image is anchored on the
    root so the garbage collector cannot reclaim it mid-display (the classic
    tk blank-image bug).
    """
    try:
        from PIL import Image, ImageTk  # noqa: PLC0415 — lazy
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if getattr(sys, 'frozen', False):
            bundle = os.path.join(os.path.dirname(sys.executable), '_internal')
            if os.path.isdir(bundle):
                base = bundle
        path = os.path.join(base, 'static', 'icons', 'logo.png')
        if not os.path.isfile(path):
            return None
        img = Image.open(path).convert('RGBA').resize((size, size),
                                                      Image.LANCZOS)
        photo = ImageTk.PhotoImage(img, master=root)
        root._tofu_logo_photo = photo
        return photo
    except Exception:
        return None


def card_frame(parent, palette):
    """A bordered 'card' container matching the web UI's component cards.

    tk.Frame (not ttk) because only tk frames take a real border color via
    highlightthickness/highlightbackground — ttk frames ignore both.
    """
    import tkinter as tk  # noqa: PLC0415 — lazy
    return tk.Frame(parent, bg=palette['bg2'],
                    highlightthickness=1,
                    highlightbackground=palette['border'],
                    highlightcolor=palette['border'])
