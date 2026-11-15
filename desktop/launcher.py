#!/usr/bin/env python3
"""Tofu Desktop Launcher — system tray + Tofu server + node links.

This is the entry point for the packaged desktop app (.exe / .app), the one
desktop artifact. Every install is a Tofu node (docs/modules/remote_execution.md
"Nodes and pairing"): with Computer Control on, it links to its own bundled
server and to every server it joined with a pairing link, each over an
outbound bridge loop (lib/desktop_agent/links.py::NodeLinks).

It spawns the real Tofu server (Quart + Hypercorn) as a child process — the
SAME code path as ``python server.py`` — so the desktop build gets the full,
correctly-initialized startup (DB bootstrap, background workers, executor
sizing, push hub). It then opens the default browser and shows a system tray
icon with controls.

Three things this launcher is careful about, because they bit us before:

1. **Windowed builds have no console.** Under PyInstaller ``console=False``,
   ``sys.stdout`` / ``sys.stderr`` are ``None``. Writing to them raises
   ``AttributeError: 'NoneType' object has no attribute 'write'``. All
   diagnostics here go through :func:`_log`, which is null-safe and tees to
   a log file next to the executable.

2. **The app is Quart, not Flask.** ``app.run(threaded=...)`` is a Flask-only
   call and raises ``TypeError`` on a Quart app. We never call it. The server
   is launched via server.py's own ``__main__`` (Hypercorn), as a subprocess.

3. **HiDPI blur.** The process must declare per-monitor DPI awareness *before*
   any window (tray icon / tk dialog) is created, or Windows bitmap-stretches
   everything.

When frozen (PyInstaller), paths resolve relative to the bundle directory.
When run from source, paths resolve relative to the project root.
"""

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

# ── Resolve base directory (frozen vs source) ──
if getattr(sys, 'frozen', False):
    # PyInstaller --onedir: the executable lives at dist/Tofu/Tofu.exe and
    # the app files are at dist/Tofu/_internal/
    BASE_DIR = os.path.join(os.path.dirname(sys.executable), '_internal')
    if not os.path.isdir(BASE_DIR):
        BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Ensure the app directory is on sys.path so `import server` / runpy works.
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from lib.paper.builds.workers import dispatch_frozen_worker  # noqa: E402
dispatch_frozen_worker()

from runtime_guards import install_process_resource_defaults  # noqa: E402
from tofu_dotenv import load_dotenv_file  # noqa: E402

# ── Portable data directory (next to the executable, not inside _internal) ──
_EXE_DIR = (os.path.dirname(sys.executable)
            if getattr(sys, 'frozen', False) else BASE_DIR)
DATA_DIR = os.path.join(_EXE_DIR, 'data')
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError:
    pass

os.environ.setdefault('TOFU_DATA_DIR', DATA_DIR)

DEFAULT_PORT = 15000

# ── Update check ──────────────────────────────────────────────────────
# The desktop app has no built-in updater, so a user on an old version never
# learns a new one shipped. On startup we do ONE best-effort query of the
# repo's latest published release and, if it is newer than the bundled
# VERSION, surface a non-blocking "download update" affordance in the tray.
# Anything that goes wrong (offline, rate-limited, parse error) is swallowed
# and logged at diagnostic level — the check must never delay or break launch.
_RELEASES_API = 'https://api.github.com/repos/rangehow/ToFu/releases/latest'
_RELEASES_PAGE = 'https://github.com/rangehow/ToFu/releases/latest'

# ── Null-safe diagnostics ─────────────────────────────────────────────
# Opened lazily; in windowed builds sys.stderr is None so the file is the
# only place these messages can land.
_LOG_PATH = os.path.join(DATA_DIR, 'desktop.log')
_log_fh = None


def _log(msg: str) -> None:
    """Write a diagnostic line without ever touching a None stream."""
    line = '[Tofu] %s\n' % msg
    global _log_fh
    if _log_fh is None:
        try:
            from lib.logging_pkg.log_retention import register_external_log
            register_external_log(_LOG_PATH, 'desktop_console')
            _log_fh = open(_LOG_PATH, 'a', encoding='utf-8', buffering=1)
        except OSError:
            _log_fh = False  # give up on the file, still try stderr
        except Exception:
            # A retention helper failure must not make a windowed launcher
            # silent; the open below remains its last-resort diagnostic sink.
            try:
                _log_fh = open(_LOG_PATH, 'a', encoding='utf-8', buffering=1)
            except OSError:
                _log_fh = False
    try:
        from lib.logging_pkg.log_record_values import text_value
        line = text_value(line, max_chars=4096)
    except Exception:
        pass
    try:
        if _log_fh:
            _log_fh.write(line)
    except OSError:
        pass
    # sys.stderr may be None (windowed) — guard explicitly.
    try:
        if sys.stderr is not None:
            sys.stderr.write(line)
    except Exception:
        pass


def _enable_dpi_awareness() -> None:
    """Mark the process per-monitor DPI-aware so HiDPI rendering is crisp.

    Must run before any window is created. No-op off Windows. The 3-level
    fallback lives in desktop._tk_theme (single theme source) — this used to
    be one of TWO private copies and the post_install one lacked the v2 level.
    """
    from desktop import _tk_theme as theme
    theme.ensure_dpi_awareness(_log)


def _find_free_port(preferred: int = DEFAULT_PORT) -> int:
    """Return the preferred port if available, else an OS-assigned free one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('127.0.0.1', preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _wait_for_server(host: str, port: int, proc: subprocess.Popen,
                     timeout: float = 90.0) -> bool:
    """Block until the server accepts connections, the child dies, or timeout.

    Cold starts (FUSE, DB bootstrap, cert reuse) can take a while, so the
    timeout is generous. Returns early with False if the child process exits.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            _log('Server process exited early with code %s' % proc.returncode)
            return False
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _server_command(port: int):
    """Build the (argv, env) for the server child process.

    Frozen: re-launch THIS executable with TOFU_RUN_SERVER=1; the guard at
    the top of :func:`main` turns that invocation into the server.
    Source:  run server.py directly with the current interpreter.
    """
    env = os.environ.copy()
    load_dotenv_file(os.path.join(BASE_DIR, '.env'), env)
    env['TOFU_PROJECT_PATH'] = _EXE_DIR
    install_process_resource_defaults(env)
    env['PORT'] = str(port)
    env['BIND_HOST'] = '127.0.0.1'
    # Plain HTTP on loopback — no self-signed cert warnings for a local app.
    env['TOFU_TLS'] = '0'
    # The desktop launcher deliberately owns and monitors this child. Bypass
    # the project-local manager handoff so the tray retains a real process
    # handle and Quit can stop exactly the server it launched.
    env['TOFU_SERVER_WORKER'] = '1'
    env['TOFU_MANAGED_BY'] = 'desktop'
    env['TOFU_EXTERNAL_CONSOLE_LOG'] = _LOG_PATH
    env['TOFU_EXTERNAL_CONSOLE_STREAM'] = 'desktop_console'
    # The in-process agent polls THIS child, a different process: hand it the
    # launcher's capability or every local poll is refused as unauthorized.
    from lib.desktop_wire.process_agent_token import child_environment
    env.update(child_environment())

    if getattr(sys, 'frozen', False):
        env['TOFU_RUN_SERVER'] = '1'
        return [sys.executable], env
    return [sys.executable, os.path.join(BASE_DIR, 'server.py')], env


def _spawn_server(port: int) -> subprocess.Popen:
    """Start the Tofu server as a child process and return the handle."""
    cmd, env = _server_command(port)

    # Tee child stdout/stderr into the desktop log (its own stderr may be a
    # dead handle in windowed mode, so give it a real file).
    try:
        try:
            from lib.logging_pkg.log_retention import register_external_log
            register_external_log(_LOG_PATH, 'desktop_console')
        except Exception:
            pass
        out = open(_LOG_PATH, 'a', encoding='utf-8', buffering=1)
    except OSError:
        out = subprocess.DEVNULL

    kwargs = dict(cwd=BASE_DIR, env=env, stdout=out, stderr=out,
                  stdin=subprocess.DEVNULL)
    if sys.platform.startswith('win'):
        # Don't flash a console window for the child.
        kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

    _log('Starting server: %s (port %d)' % (cmd, port))
    return subprocess.Popen(cmd, **kwargs)


def _load_icon():
    """Load the tray icon image (falls back to a generated square)."""
    from PIL import Image

    for name in ('tofu.ico', 'tofu.icns', 'logo.png'):
        icon_path = os.path.join(BASE_DIR, 'static', 'icons', name)
        if os.path.isfile(icon_path):
            try:
                return Image.open(icon_path)
            except Exception as e:
                _log('Icon load failed for %s: %s' % (name, e))
                continue

    img = Image.new('RGBA', (64, 64), (255, 215, 0, 255))
    return img


def _local_version() -> str:
    """Return the bundled app version (from the VERSION file), or '' if unknown."""
    try:
        from lib.version import __version__ as v
        return (v or '').strip()
    except Exception as e:
        _log('Could not read local version: %s' % e)
        return ''


def _parse_version(v: str):
    """Parse a version string into a comparable tuple of ints.

    Strips a leading 'v' and any pre-release suffix (e.g. '0.14.1-beta' →
    (0, 14, 1)). Returns None when nothing numeric can be parsed.
    """
    if not v:
        return None
    core = v.lstrip('vV').split('-', 1)[0].split('+', 1)[0]
    parts = []
    for chunk in core.split('.'):
        digits = ''.join(ch for ch in chunk if ch.isdigit())
        if digits == '':
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


def _fetch_latest_version(timeout: float = 6.0):
    """Best-effort fetch of the latest published release tag from GitHub.

    Returns the tag string (e.g. 'v0.14.1') or None on any failure. Never
    raises. Drafts are excluded by the /releases/latest endpoint itself.
    """
    import json
    import urllib.request
    req = urllib.request.Request(
        _RELEASES_API,
        headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'Tofu-Desktop'},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8', 'replace'))
        tag = (data.get('tag_name') or data.get('name') or '').strip()
        return tag or None
    except Exception as e:
        _log('Update check failed (non-fatal): %s' % e)
        return None


def _check_for_update():
    """Compare local vs latest release. Returns the newer tag string or None.

    None means "no known newer version" (up to date, offline, or unparseable)
    — the caller treats that as "show nothing", so a failed check is silent.
    """
    local = _parse_version(_local_version())
    latest_tag = _fetch_latest_version()
    latest = _parse_version(latest_tag or '')
    if local is None or latest is None:
        return None
    if latest > local:
        _log('Update available: local=%s latest=%s' % (_local_version(), latest_tag))
        return latest_tag
    return None


def _node_links(state: dict):
    """The node's link set, created on first use with the live perms dict."""
    links = state.get('links')
    if links is None:
        from lib.desktop_agent.links import NodeLinks
        from lib.desktop_agent._permissions import safe_default
        if not isinstance(state.get('perms'), dict):
            state['perms'] = safe_default()
        links = state['links'] = NodeLinks(state['perms'], _log)
    return links


def _start_computer_control(port: int, state: dict) -> None:
    """Start this node: link to the bundled server and every joined server.

    Safety posture is **deny by default**: enabling computer control grants
    only READ-ONLY tools (list/read files, system overview) to every linked
    server. The write / exec / GUI tiers each stay OFF until the user ticks
    them. Every link reads the SAME ``permissions`` dict each poll and
    re-declares it, so the server and the device both see a toggle on the
    next command. The bundled server is reached with the process capability;
    joined servers with their own paired credential.
    """
    if state.get('enabled'):
        return
    try:
        links = _node_links(state)
    except Exception as e:
        _log('Computer control unavailable (import failed): %s' % e)
        state['error'] = str(e)
        return
    from lib.bridge_auth import process_agent_token
    state['error'] = None
    links.start(local=(f'http://127.0.0.1:{port}', process_agent_token()))
    state['enabled'] = True
    _log('Computer control ENABLED (perms=%s, links=%d)'
         % (state['perms'], len(links.statuses())))


def _stop_computer_control(state: dict) -> None:
    """Stop every link of this node."""
    links = state.get('links')
    if links is not None:
        links.stop()
    state['enabled'] = False
    _log('Computer control DISABLED')


def _diag_report(state: dict) -> str:
    """The copyable diagnostics bundle for a dead link, pasteable into a chat.

    Every joined server's route, candidates and live verdict plus the log
    tail; credentials are reported as presence + length only.
    """
    lines = ['Tofu node diagnostics — %s' % time.strftime('%Y-%m-%d %H:%M:%S'),
             'version: %s' % (_local_version() or '?'),
             'platform: %s' % sys.platform]
    links = {row['id']: row for row in _node_links(state).statuses()}
    try:
        from lib.desktop_agent.config import attachments, config_path
        lines.append('config: %s' % config_path())
        for record in attachments():
            secret = record.get('secret') or ''
            row = links.get(record['id']) or {}
            lines.append('server %s: url=%s candidates=%s state=%s secret=%s link=%s'
                         % (record.get('label'), record.get('url'),
                            record.get('candidates') or [], record.get('state'),
                            'set (%d chars)' % len(secret) if secret else 'none',
                            row.get('last_status') or '(no status yet)'))
    except Exception as e:
        lines.append('config unreadable: %s' % e)
    local = links.get('local')
    if local is not None:
        lines.append('local link: %s' % (local.get('last_status') or '(no status yet)'))
    lines.append('log file: %s' % _LOG_PATH)
    try:
        with open(_LOG_PATH, encoding='utf-8', errors='replace') as f:
            tail = f.readlines()[-120:]
    except OSError as e:
        lines.append('(log unreadable: %s)' % e)
    else:
        lines.append('--- last %d log lines ---' % len(tail))
        lines.extend(ln.rstrip('\n') for ln in tail)
    return '\n'.join(lines)


def _join_server(port: int, state: dict, text: str, stop=None):
    """Redeem a pairing link; joining also turns this node on."""
    from lib.desktop_agent._run import _ensure_agent_id
    record = _node_links(state).join(text, node_id=_ensure_agent_id(),
                                     name=socket.gethostname(), stop=stop)
    if not state.get('enabled'):
        _start_computer_control(port, state)
        _persist_cc_state(state)
    return record


def _persist_cc_state(state: dict) -> None:
    """Write the tray's computer-control state to the agent config.

    Called ONLY from explicit user clicks (the enable toggle, a permission
    tier) — never from quit/startup paths, so a crash or a normal Quit
    cannot erase what the user chose. Failures are logged, never fatal:
    a read-only config dir must not break the toggle itself.
    """
    try:
        from lib.desktop_agent.config import save_computer_control
        save_computer_control(bool(state.get('enabled')),
                              state.get('perms') or {})
    except Exception as e:
        _log('Could not persist computer-control state: %s' % e)


def _restore_cc_state(state: dict) -> bool:
    """Load the persisted computer-control state into *state*.

    Returns whether the agent should be auto-started. A fresh install (or
    a malformed blob) yields False and leaves *state* untouched —
    deny-by-default is the floor this restore never lowers. Saved perms
    are merged over the deny-all baseline so a tier added by a future
    version still defaults OFF for old config files.
    """
    try:
        from lib.desktop_agent.config import load_computer_control
        enabled, saved_perms = load_computer_control()
    except Exception as e:
        _log('Could not read computer-control state: %s' % e)
        return False
    if saved_perms:
        try:
            from lib.desktop_agent._permissions import safe_default
            perms = safe_default()
            perms.update(saved_perms)
            state['perms'] = perms
        except Exception as e:
            _log('Could not merge saved permissions: %s' % e)
    return enabled


def _open_path(path: str) -> None:
    """Open a local file with the platform's default viewer."""
    if sys.platform.startswith('win'):
        os.startfile(path)  # noqa: S606 — our own log file, no user input
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['xdg-open', path])


def _run_tray(port: int, proc: subprocess.Popen):
    """Run the system tray icon (blocks on the main thread)."""
    url = f'http://127.0.0.1:{port}'
    # Desktop-control agent state (started/stopped via the tray toggle below).
    # 'perms' is the live, deny-all-by-default permissions dict shared with the
    # agent loop; the per-tier toggles mutate it in place.
    _cc_state: dict = {'enabled': False, 'thread': None, 'stop': None,
                       'error': None, 'perms': None}

    # Restore the user's persisted choice: an agent they explicitly enabled
    # comes back up on every launch; a fresh install stays OFF.
    if _restore_cc_state(_cc_state):
        _log('Computer control was enabled at last exit — restoring')
        _start_computer_control(port, _cc_state)

    def _shutdown():
        _stop_computer_control(_cc_state)
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    try:
        import pystray
        from pystray import MenuItem
    except ImportError:
        _log('pystray not available — running without system tray. '
             'Server is at %s' % url)
        try:
            while proc.poll() is None:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        _shutdown()
        return

    icon_image = _load_icon()

    # Holder mutated by the background update check; the tray reads it lazily.
    _update = {'tag': None}

    def on_open(icon, item):
        webbrowser.open(url)

    def on_browser_relay(url):
        def _handler(icon, item):
            webbrowser.open(url.rstrip('/') + '/#tofu-agent-relay')
        return _handler

    def on_join(icon, item):
        from desktop import _tk_host
        from desktop.connect_ui import prompt_join
        _tk_host.call(lambda: prompt_join(
            lambda text, stop: _join_server(port, _cc_state, text, stop), log=_log))
        try:
            icon.update_menu()
        except Exception as e:
            _log('Could not refresh tray menu after join: %s' % e)

    def on_leave(attachment_id):
        def _handler(icon, item):
            _node_links(_cc_state).leave(attachment_id)
            try:
                icon.update_menu()
            except Exception as e:
                _log('Could not refresh tray menu after leave: %s' % e)
        return _handler

    def _link_rows():
        try:
            return _node_links(_cc_state).statuses()
        except Exception as e:
            _log('Could not read node links: %s' % e)
            return []

    def on_toggle_autostart(icon, item):
        from desktop import autostart
        try:
            autostart.apply(not autostart.enabled())
        except Exception as e:
            _log('Could not change login startup: %s' % e)

    def on_update(icon, item):
        webbrowser.open(_RELEASES_PAGE)

    def on_components(icon, item):
        """Launch the component manager (select + progress-visible install)."""
        try:
            from desktop.post_install import OPTIONAL_COMPONENTS, _prompt_gui
        except Exception as e:
            _log('Component installer unavailable: %s' % e)
            return
        not_installed = [c for c in OPTIONAL_COMPONENTS if not c.is_installed()]
        if not not_installed:
            return
        # The dialog hosts the install itself (progress rows + bar) and
        # returns the results; nothing downloads invisibly anymore. The
        # DIALOG rides the tk host thread (tray-first topology); the rest
        # of this handler stays on the tray thread.
        from desktop import _tk_host
        results = _tk_host.call(lambda: _prompt_gui(not_installed))
        for name, success, msg in results:
            _log('%s %s: %s' % ('OK' if success else 'FAIL', name, msg))

    def on_toggle_computer_control(icon, item):
        """Enable/disable the in-process desktop-control agent."""
        if _cc_state.get('enabled'):
            _stop_computer_control(_cc_state)
        else:
            try:
                _start_computer_control(port, _cc_state)
            except Exception as e:
                # The role window renders state['error']; a raise that only
                # reached the log left the switch silently off.
                _log('Computer control failed to start: %s' % e)
                _cc_state['error'] = str(e) or type(e).__name__
        _persist_cc_state(_cc_state)
        try:
            icon.update_menu()
        except Exception as e:
            _log('Could not refresh tray menu after CC toggle: %s' % e)

    def _toggle_perm(key: str):
        """Flip one permission tier on the live shared perms dict."""
        def _handler(icon, item):
            perms = _cc_state.get('perms')
            if not isinstance(perms, dict):
                # Not enabled yet — ticking a tier has nothing to mutate.
                return
            perms[key] = not perms.get(key)
            _log('Computer control tier %s -> %s' % (key, perms[key]))
            _persist_cc_state(_cc_state)
            try:
                icon.update_menu()
            except Exception as e:
                _log('Could not refresh tray menu after perm toggle: %s' % e)
        return _handler

    def _perm_checked(key: str):
        return lambda item: bool((_cc_state.get('perms') or {}).get(key))

    def _perm_enabled(item):
        # Tier toggles are only meaningful while the node is running.
        return bool(_cc_state.get('enabled'))

    def _server_menu_items():
        from desktop import role_window
        rows = role_window.server_rows(_link_rows(), _lang)
        if not rows:
            return [MenuItem(_tt('desktop.tray.noServers'), None, enabled=False)]
        urls = {row['id']: row.get('url') or '' for row in _link_rows()}
        return [MenuItem(row['text'], pystray.Menu(
            MenuItem(_tt('desktop.tray.browserRelay'),
                     on_browser_relay(urls.get(row['id']) or row['label'])),
            MenuItem(_tt('desktop.tray.leave', label=row['label']),
                     on_leave(row['id'])),
        )) for row in rows]

    def _autostart_supported():
        from desktop import autostart
        return autostart.supported()

    def _autostart_on():
        from desktop import autostart
        try:
            return autostart.enabled()
        except Exception as e:
            _log('Could not read login startup: %s' % e)
            return False

    def on_quit(icon, item):
        _stop_computer_control(_cc_state)
        icon.stop()
        _shutdown()
        os._exit(0)

    # Tray strings are bilingual (desktop.tray.* in _tk_theme) — the AST
    # ratchet in tests/desktop/test_desktop_tray_i18n.py refuses a hardcoded literal.
    from desktop import _tk_theme as theme
    _lang = theme.detect_lang()

    def _tt(key: str, **fill) -> str:
        text = theme.t(key, _lang)
        for ph, val in fill.items():
            text = text.replace('{%s}' % ph, str(val))
        return text

    # ── Role window / control panel (desktop/role_window.py) ──
    # The window delegates every mutation to the SAME handlers the tray
    # uses (a second VIEW over _cc_state, not a new state path), so the
    # two surfaces can never disagree. Tray callbacks expect an icon
    # with update_menu(); window-driven actions have no tray to
    # refresh, hence the null shim.
    class _NullIcon:
        @staticmethod
        def update_menu():
            pass

    _NULL_ICON = _NullIcon()

    _server_seen = {'ready': False}

    def _server_state():
        code = proc.poll()
        if code is not None:
            return 'stopped', code
        if not _server_seen['ready']:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                    _server_seen['ready'] = True
            except OSError:
                return 'starting', None
        return 'running', None

    def _role_state_fn():
        from desktop import autostart, role_window
        server_state, exit_code = _server_state()
        rows = _link_rows()
        local = next((r for r in rows if r.get('local')), {})
        cc_view = dict(_cc_state, last_status=local.get('last_status') or {})
        return role_window.role_state_full(
            port, cc_view, rows,
            show_flag=role_window.should_show_at_startup(),
            server_state=server_state, server_exit_code=exit_code,
            autostart=autostart.enabled() if autostart.supported() else None)

    _role_actions = {
        'open': lambda: webbrowser.open(url),
        'open_log': lambda: _open_path(_LOG_PATH),
        'toggle_cc': lambda: on_toggle_computer_control(_NULL_ICON, None),
        'toggle_perm': lambda key: _toggle_perm(key)(_NULL_ICON, None),
        'join': lambda: on_join(_NULL_ICON, None),
        'leave': lambda attachment_id: on_leave(attachment_id)(_NULL_ICON, None),
        'toggle_autostart': lambda: on_toggle_autostart(_NULL_ICON, None),
        'copy_diag': lambda: _diag_report(_cc_state),
    }

    def on_control_panel(icon, item):
        from desktop import _tk_host, role_window
        _tk_host.post_or_call(lambda: role_window.show_role_window(
            _role_state_fn, _role_actions, log=_log))

    # Dynamic "update available" item: its text is computed at menu-open time
    # and it is hidden entirely until the background check finds a newer tag.
    menu = pystray.Menu(
        MenuItem(_tt('desktop.tray.open'), on_open, default=True),
        MenuItem(_tt('desktop.tray.controlPanel'), on_control_panel),
        MenuItem(lambda item: _tt('desktop.tray.downloadUpdate',
                                  tag=_update['tag']),
                 on_update,
                 visible=lambda item: bool(_update['tag'])),
        pystray.Menu.SEPARATOR,
        MenuItem(_tt('desktop.tray.enableCC'), on_toggle_computer_control,
                 checked=lambda item: bool(_cc_state.get('enabled'))),
        MenuItem(_tt('desktop.tray.permissions'), pystray.Menu(
            MenuItem(_tt('desktop.tray.permWrite'), _toggle_perm('allow_write'),
                     checked=_perm_checked('allow_write'), enabled=_perm_enabled),
            MenuItem(_tt('desktop.tray.permExec'), _toggle_perm('allow_exec'),
                     checked=_perm_checked('allow_exec'), enabled=_perm_enabled),
            MenuItem(_tt('desktop.tray.permGui'), _toggle_perm('allow_gui'),
                     checked=_perm_checked('allow_gui'), enabled=_perm_enabled),
        )),
        MenuItem(_tt('desktop.tray.join'), on_join),
        MenuItem(_tt('desktop.tray.servers'), pystray.Menu(_server_menu_items)),
        MenuItem(_tt('desktop.tray.autostart'), on_toggle_autostart,
                 checked=lambda item: _autostart_on(),
                 visible=lambda item: _autostart_supported()),
        MenuItem(_tt('desktop.tray.installComponents'), on_components),
        pystray.Menu.SEPARATOR,
        MenuItem(_tt('desktop.tray.quit'), on_quit),
    )

    icon = pystray.Icon('tofu', icon_image, 'Tofu', menu)

    # Startup role declaration (owner directive 2026-08-03), now TRAY-FIRST
    # (owner report 2026-08-04): the tray must exist BEFORE the window can
    # ever hide, or "minimize to tray" strands the app with no surface. The
    # tk host thread owns the window; the main thread reaches icon.run()
    # immediately. Off win32 host.start() returns False and post_or_call
    # inlines — the legacy window-then-tray sequence stands there.
    from desktop import _tk_host, role_window
    _tk_host.start(log=_log)
    if role_window.should_show_at_startup():
        _tk_host.post_or_call(lambda: role_window.show_role_window(
            _role_state_fn, _role_actions, log=_log))

    # Kick off the update check off the main thread so it never delays the
    # tray appearing. When it finds a newer version it flips the holder and
    # asks pystray to re-render the menu (the item then becomes visible).
    def _bg_update_check():
        tag = _check_for_update()
        if tag:
            _update['tag'] = tag
            try:
                icon.update_menu()
            except Exception as e:
                _log('Could not refresh tray menu after update check: %s' % e)

    threading.Thread(target=_bg_update_check, daemon=True,
                     name='tofu-update-check').start()

    icon.run()
    # Tray stopped (e.g. Quit) — make sure the server goes down too.
    _shutdown()


def main():
    # ── Server mode (frozen self-relaunch) ──
    # When this executable is started with TOFU_RUN_SERVER=1 it IS the server:
    # execute server.py under __main__ so we reuse its full Hypercorn startup.
    if os.environ.get('TOFU_RUN_SERVER') == '1':
        import runpy
        runpy.run_module('server', run_name='__main__')
        return

    # ── Playwright-install mode (frozen self-relaunch) ──
    # A PyInstaller --onedir bundle has NO standalone python.exe — the only
    # executable is THIS one. So `sys.executable -m playwright install` cannot
    # work (main() would just boot a second app). Instead post_install.py
    # relaunches us with TOFU_PLAYWRIGHT_INSTALL=1, and here we drive the
    # bundled playwright package in-process to fetch its Chromium binary.
    #
    # --only-shell matches install.sh + post_install.py: a default install
    # also fetches the 175 MB full build, which no call site here launches
    # (every one is headless).
    if os.environ.get('TOFU_PLAYWRIGHT_INSTALL') == '1':
        try:
            from playwright.__main__ import main as _pw_main
            sys.argv = ['playwright', 'install', '--only-shell', 'chromium']
            _pw_main()
        except SystemExit:
            raise
        except Exception as e:
            _log('Playwright install failed: %s' % e)
            sys.exit(1)
        return

    # ── Smoke mode (CI: prove the frozen bundle can actually START) ──
    # The release gates check that an artifact EXISTS and is of a plausible
    # SIZE. Neither can see a missing hidden import: tofu.spec declares 48 of
    # them, and dropping one changes no byte count and no PyInstaller exit
    # code — it fails at the moment the user double-clicks, with a
    # ModuleNotFoundError nobody is around to read.
    #
    # So CI runs the built binary once with TOFU_SMOKE=1. This imports
    # `server`, which at MODULE level constructs the Quart app and calls
    # routes.register_all(app) — so a single import exercises the whole
    # blueprint tree and the transitive dependency graph the spec's
    # hiddenimports exist to preserve. Then it exits.
    #
    # ── The verdict is the EXIT CODE, deliberately, and this is the trap ──
    # "the process stayed alive for N seconds" is NOT a usable signal for this
    # build: `console=False` means a windowed binary detaches and lingers
    # regardless of whether anything imported, so a liveness check is green by
    # construction — a guard measuring the wrong thing. An exit code cannot be
    # faked that way: 0 only happens if every import resolved.
    #
    # No socket is bound and no server is served: binding would make the check
    # sensitive to port contention on shared CI runners, which is noise about
    # the environment rather than evidence about the bundle.
    if os.environ.get('TOFU_SMOKE') == '1':
        try:
            # A plain module import skips server.py's __main__ prologue,
            # which the real TOFU_RUN_SERVER child runs; import its
            # dependency here (a module-level fcntl import in it once
            # crashed every Windows launch while this smoke stayed green).
            import serverctl  # noqa: F401
            import server as _server
            app = getattr(_server, 'app', None)
            if app is None:
                raise RuntimeError('server module exposes no `app` object')
            # register_all() ran at import; an empty blueprint map would mean
            # the app booted hollow, which an import-only check would
            # otherwise call success.
            n = len(getattr(app, 'blueprints', {}) or {})
            if n == 0:
                raise RuntimeError('no blueprints registered on the app')
            sys.stdout.write('TOFU_SMOKE_OK version=%s blueprints=%d\n'
                             % (_local_version() or 'unknown', n))
            sys.stdout.flush()
        except BaseException:
            # Print the real traceback to stderr and fail loudly. CI asserts
            # BOTH exit==0 and an empty stderr, so a degraded-but-surviving
            # import cannot pass as healthy.
            import traceback
            traceback.print_exc()
            sys.exit(1)
        return

    # ── GUI process: crisp rendering before any window is created ──
    _enable_dpi_awareness()

    port = _find_free_port()

    # Start the real server as a child process (full Hypercorn startup).
    try:
        proc = _spawn_server(port)
    except Exception as e:
        _log('FATAL: could not start server process: %s' % e)
        raise

    # Open the browser as soon as the server is reachable — in a background
    # thread so the optional first-launch prompt can run in parallel.
    url = f'http://127.0.0.1:{port}'

    def _open_when_ready():
        if _wait_for_server('127.0.0.1', port, proc):
            _log('Server ready at %s' % url)
            webbrowser.open(url)
        else:
            _log('WARNING: server did not become ready at %s' % url)

    threading.Thread(target=_open_when_ready, daemon=True,
                     name='tofu-open-browser').start()

    # ── First launch: offer optional component downloads ──
    try:
        from desktop.post_install import is_first_launch, run_first_launch_prompt
        if is_first_launch():
            run_first_launch_prompt()
    except Exception as e:
        _log('Component prompt skipped: %s' % e)

    # Run tray on the main thread (required by macOS/Windows).
    _run_tray(port, proc)


if __name__ == '__main__':
    main()
