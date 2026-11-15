"""The packaged Windows app must import without POSIX-only stdlib modules.

Incident 2026-11-10: the frozen Tofu.exe server child died at boot with
``ModuleNotFoundError: No module named 'fcntl'`` because ``server.py``'s
``__main__`` prologue imports ``serverctl``, which imported ``fcntl`` at
module level. The CI smoke (``TOFU_SMOKE``) imports ``server`` as a plain
module, so that ``__main__``-only import was never exercised on Windows.

These checks run on Linux by hiding the POSIX-only modules Windows lacks,
then importing every module the desktop launcher and the server's
``__main__`` prologue load before the application is built.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHER = _ROOT / 'desktop' / 'launcher.py'

# Stdlib modules that exist on POSIX and are absent from Windows CPython.
_POSIX_ONLY = ('fcntl', 'termios', 'pwd', 'grp', 'resource')

_DESKTOP_BOOT_MODULES = (
    'serverctl',
    'bootstrap_pkg.server_entry',
    'lib.server.server_boot_env',
    'desktop.launcher',
    'desktop.role_window',
    'lib.desktop_agent.links',
    'lib.bridge_auth',
    'lib.desktop_wire.process_agent_token',
    'lib.desktop_agent',
)

_PROBE = r'''
import importlib, sys
blocked = set(sys.argv[1].split(','))
for name in blocked:
    sys.modules.pop(name, None)

class _WindowsStdlib:
    def find_spec(self, name, path=None, target=None):
        if name in blocked:
            raise ModuleNotFoundError("No module named %r" % name, name=name)
        return None

sys.meta_path.insert(0, _WindowsStdlib())
failures = []
for module in sys.argv[2:]:
    try:
        importlib.import_module(module)
    except Exception as exc:
        failures.append('%s: %r' % (module, exc))
print('\n'.join(failures))
sys.exit(1 if failures else 0)
'''


def test_desktop_boot_modules_import_without_posix_only_stdlib():
    env = dict(os.environ, PYTHONPATH=str(_ROOT))
    proc = subprocess.run(
        [sys.executable, '-c', _PROBE, ','.join(_POSIX_ONLY),
         *_DESKTOP_BOOT_MODULES],
        cwd=_ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, (
        'modules on the Windows desktop boot path import a POSIX-only '
        'stdlib module at module level (import it lazily inside the POSIX '
        'branch instead):\n' + proc.stdout + proc.stderr[-2000:])


def test_smoke_branch_imports_the_server_main_prologue():
    """TOFU_SMOKE imports ``server`` as a module, which skips its
    ``if __name__ == '__main__'`` prologue; the smoke branch must import that
    prologue's dependency itself or a Windows-only failure there ships."""
    src = _LAUNCHER.read_text(encoding='utf-8')
    branch = src[src.index("os.environ.get('TOFU_SMOKE')"):]
    branch = branch[:branch.index('_enable_dpi_awareness()')]
    code = '\n'.join(line for line in branch.splitlines()
                     if not line.strip().startswith('#'))
    assert 'import serverctl' in code
