#!/usr/bin/env python3
"""Tofu Desktop Launcher — system tray + Flask server + auto-open browser.

This is the entry point for the packaged desktop app (.exe / .app).
It starts the Flask server in a background thread, opens the default
browser, and provides a system tray icon with controls.

When frozen (PyInstaller), paths resolve relative to the bundle directory.
When run from source, paths resolve relative to the project root.
"""

import os
import sys
import socket
import threading
import time
import webbrowser

# ── Resolve base directory (frozen vs source) ──
if getattr(sys, 'frozen', False):
    # PyInstaller sets sys._MEIPASS for --onefile, but we use --onedir
    # so the executable lives at dist/Tofu/Tofu.exe and the app files
    # are at dist/Tofu/_internal/
    BASE_DIR = os.path.join(os.path.dirname(sys.executable), '_internal')
    if not os.path.isdir(BASE_DIR):
        BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Ensure the app directory is on sys.path so `import server` works
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# ── Portable data directory ──
# Store all runtime data next to the executable (not buried in _internal)
DATA_DIR = os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False)
                        else BASE_DIR, 'data')
os.makedirs(DATA_DIR, exist_ok=True)

# Point the app's config/database to the portable data dir
os.environ.setdefault('TOFU_DATA_DIR', DATA_DIR)

DEFAULT_PORT = 15000


def _find_free_port(preferred: int = DEFAULT_PORT) -> int:
    """Return the preferred port if available, else find a free one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('127.0.0.1', preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _wait_for_server(host: str, port: int, timeout: float = 30.0) -> bool:
    """Block until the server accepts connections or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _start_server(port: int):
    """Import and run the Flask app (blocks forever)."""
    os.environ['PORT'] = str(port)
    os.environ['BIND_HOST'] = '127.0.0.1'

    # Suppress the Werkzeug reloader — it doesn't work in frozen apps
    os.environ['WERKZEUG_RUN_MAIN'] = 'true'

    # Change to the app directory so Flask finds templates/static
    os.chdir(BASE_DIR)

    try:
        from server import app
        app.run(host='127.0.0.1', port=port, debug=False, threaded=True,
                use_reloader=False)
    except Exception as e:
        sys.stderr.write(f'[Tofu] Server failed to start: {e}\n')
        os._exit(1)


def _load_icon():
    """Load the tray icon image."""
    from PIL import Image

    # Try platform-native format first
    for name in ('tofu.ico', 'tofu.icns', 'logo.png'):
        icon_path = os.path.join(BASE_DIR, 'static', 'icons', name)
        if os.path.isfile(icon_path):
            try:
                return Image.open(icon_path)
            except Exception:
                continue

    # Fallback: generate a simple colored square
    img = Image.new('RGBA', (64, 64), (255, 215, 0, 255))
    return img


def _run_tray(port: int):
    """Run the system tray icon (blocks on the main thread)."""
    try:
        import pystray
        from pystray import MenuItem
    except ImportError:
        sys.stderr.write(
            '[Tofu] pystray not available — running without system tray.\n'
            '       The server is at http://127.0.0.1:%d\n' % port)
        # Just block forever
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return

    url = f'http://127.0.0.1:{port}'
    icon_image = _load_icon()

    def on_open(icon, item):
        webbrowser.open(url)

    def on_components(icon, item):
        """Launch the component installer dialog."""
        from desktop.post_install import OPTIONAL_COMPONENTS, _prompt_gui, _install_components
        not_installed = [c for c in OPTIONAL_COMPONENTS if not c.is_installed()]
        if not not_installed:
            return
        selected = _prompt_gui(not_installed)
        if selected:
            def _bg():
                results = _install_components(selected)
                for name, success, msg in results:
                    status = '✓' if success else '✗'
                    sys.stderr.write(f'[Tofu] {status} {name}: {msg}\n')
            threading.Thread(target=_bg, daemon=True).start()

    def on_quit(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        MenuItem('Open Tofu', on_open, default=True),
        pystray.Menu.SEPARATOR,
        MenuItem('Install Components...', on_components),
        MenuItem(f'Port: {port}', None, enabled=False),
        pystray.Menu.SEPARATOR,
        MenuItem('Quit', on_quit),
    )

    icon = pystray.Icon('tofu', icon_image, 'Tofu', menu)
    icon.run()


def main():
    # ── First-launch: offer optional component downloads ──
    try:
        from desktop.post_install import is_first_launch, run_first_launch_prompt
        if is_first_launch():
            run_first_launch_prompt()
    except Exception as e:
        sys.stderr.write(f'[Tofu] Component prompt skipped: {e}\n')

    port = _find_free_port()

    # Start Flask in a daemon thread
    server_thread = threading.Thread(target=_start_server, args=(port,),
                                     daemon=True, name='tofu-server')
    server_thread.start()

    # Wait for the server to come up, then open browser
    url = f'http://127.0.0.1:{port}'
    if _wait_for_server('127.0.0.1', port):
        sys.stderr.write(f'[Tofu] Server ready at {url}\n')
        webbrowser.open(url)
    else:
        sys.stderr.write(f'[Tofu] WARNING: Server did not respond within 30s at {url}\n')

    # Run tray on the main thread (required by macOS/Windows)
    _run_tray(port)


if __name__ == '__main__':
    main()
