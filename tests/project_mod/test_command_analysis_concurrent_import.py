"""A first import of ``config`` concurrent with a first import of ``command_analysis`` completes.

Thirty-odd modules outside the package import ``lib.project_mod.config``
first. CPython holds that child's import lock while the absent package's
``__init__`` runs, and ``__init__`` reaches ``run_command``, which imports
names from ``command_analysis``. A second thread that first-imports
``command_analysis`` in that window used to block on the ``config`` lock from
``command_analysis``'s own module-level import; CPython broke the lock cycle by
handing ``run_command`` a partially initialized ``command_analysis`` (an
execution host failed with ``cannot import name '_bound_scan_segments'``).
``command_analysis`` now imports only leaf modules at module level.

Each case runs in a fresh interpreter: the ``config`` thread is held as the
package ``__init__`` starts executing, the other thread starts its import and
the held thread resumes once it waits on a lock the held thread owns (or has
finished); both imports must then succeed.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_CHILD = r'''
import importlib._bootstrap as bootstrap
import importlib.abc
import sys
import threading
import time
import traceback

HOLD, FIRST, ENTRY = sys.argv[1:4]
held = threading.Event()
release = threading.Event()
outcome = {}


class _HoldingLoader:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module):
        held.set()
        release.wait(120)
        self._inner.exec_module(module)


class HoldFirstThread(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != HOLD or threading.current_thread().name != 'first':
            return None
        for finder in sys.meta_path:
            if finder is not self and hasattr(finder, 'find_spec'):
                spec = finder.find_spec(fullname, path, target)
                if spec is not None:
                    spec.loader = _HoldingLoader(spec.loader)
                    return spec
        return None


def run(name, statement):
    try:
        exec(statement, {})
        outcome[name] = 'OK'
    except BaseException as error:
        outcome[name] = ''.join(traceback.format_exception(error))


def waits_for(thread, owner):
    locks = (getattr(bootstrap, '_blocking_on', None) or {}).get(thread.ident) or ()
    if not isinstance(locks, list):
        locks = [locks]
    return any(getattr(lock, 'owner', None) == owner.ident for lock in list(locks))


import lib.log  # noqa: E402,F401  -- loaded by every host before either import
sys.meta_path.insert(0, HoldFirstThread())
first = threading.Thread(target=run, args=('first', FIRST), name='first')
first.start()
if not held.wait(120):
    sys.exit(f'first thread never reached {HOLD}')
entry = threading.Thread(target=run, args=('entry', ENTRY), name='entry')
entry.start()
deadline = time.monotonic() + 60
while entry.is_alive() and not waits_for(entry, first) and time.monotonic() < deadline:
    time.sleep(0.01)
release.set()
for thread in (first, entry):
    thread.join(120)
    if thread.is_alive():
        print(thread.name.upper(), 'HUNG', flush=True)
        import os
        os._exit(3)
for name in ('first', 'entry'):
    print(name.upper(), 'OK' if outcome[name] == 'OK' else 'FAILED')
    if outcome[name] != 'OK':
        sys.stderr.write(outcome[name])
'''

_CASES = [
    'import lib.project_mod.command_analysis',
    'from lib.project_mod.command_analysis import _is_destructive_command',
]


@pytest.mark.parametrize('entry', _CASES)
def test_command_analysis_first_import_concurrent_with_config_first_import(entry):
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(root)
    result = subprocess.run(
        [sys.executable, '-c', _CHILD, 'lib.project_mod', 'import lib.project_mod.config', entry],
        cwd=root, env=environment, capture_output=True, text=True, timeout=300)
    output = result.stdout + result.stderr[-4000:]
    assert result.returncode == 0, output
    assert 'FIRST OK' in result.stdout, output
    assert 'ENTRY OK' in result.stdout, output
