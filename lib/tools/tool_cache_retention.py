"""Evict-only retention of the shared tool cache (``tool_cache_root``).

Entry points: ``schedule_prune(cache_root)`` after a command that ran an
installer finished (run_command); ``prune(cache_root)`` runs the tools' own
reclamation synchronously; ``invokes_package_installer(command)`` recognizes a
command that ran uv or pip; ``command_uv_cache(cache_root)`` names the uv cache
commands use. Dependencies: ``lib.tools.process_environment`` and the uv/pip
executables. Policy: docs/RESOURCE_CONTROL.md ("Filesystem and Git artifact
lifecycles", "Tool caches are pruned by their tools after installs").

Only the tools decide what is reclaimable: ``uv cache prune`` (dangling
archives, stale buckets, cached environments) and ``pip cache purge`` (pip's
only reclamation). uv refuses while another uv process holds its cache lock;
``UV_LOCK_TIMEOUT=0`` turns that wait into an immediate refusal, so a prune
never waits for, delays or blocks any user command, and the next install
retries. Installed uv tools, managed Pythons and npm data are never touched.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading

from lib.log import get_logger

logger = get_logger(__name__)

# uv holds a shared lock on its cache for the whole life of every `uv run`
# process, and its prune needs the exclusive lock. Vendored MCP launchers are
# long-lived `uv run` processes on the `uv` cache, so a cache shared with them
# could never be pruned while Tofu runs; commands get their own uv cache. The
# launcher cache is not pruned here: uv's prune also deletes cached `uv run`
# environments, which would undo every launcher warm.
COMMAND_UV_CACHE = 'uv-commands'

_INSTALLER = re.compile(
    r'(?:^|[\s;&|()`/])(?:uvx?|pip[0-9.]*)(?=\s|$)|(?:^|\s)-m\s+pip(?=\s|$)')

_lock = threading.Lock()
_running: set[str] = set()
_pending: set[str] = set()


def command_uv_cache(cache_root: str) -> str:
    return os.path.join(os.path.abspath(cache_root), COMMAND_UV_CACHE)


def invokes_package_installer(command: str) -> bool:
    """Whether COMMAND ran uv, uvx or pip (each can fill the package caches)."""
    return bool(command) and bool(_INSTALLER.search(command))


def _environment(cache_root: str) -> dict[str, str]:
    from lib.tools.process_environment import apply_tool_paths, inherited_child_environment
    environment = inherited_child_environment()
    for key in ('UV_CACHE_DIR', 'PIP_CACHE_DIR', 'XDG_CACHE_HOME', 'PIP_REQUIRE_VIRTUALENV'):
        environment.pop(key, None)
    apply_tool_paths(environment, cache_root=cache_root)
    environment['UV_LOCK_TIMEOUT'] = '0'
    return environment


def _run(argv: list[str], environment: dict[str, str]) -> dict:
    completed = subprocess.run(argv, env=environment, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, check=False)
    output = (completed.stderr or completed.stdout or '').strip().splitlines()
    return {'argv': argv, 'exit': completed.returncode, 'detail': output[-1] if output else ''}


def prune(cache_root: str) -> list[dict]:
    """Run each tool's own prune on the caches under CACHE_ROOT; return the runs."""
    from lib.tools.process_environment import resolve_executable
    root = os.path.abspath(cache_root)
    environment = _environment(root)
    runs = []
    uv = resolve_executable('uv', environment)
    cache = os.path.join(root, COMMAND_UV_CACHE)
    if uv and os.path.isdir(cache):
        runs.append(_run([uv, 'cache', 'prune', '--cache-dir', cache], environment))
    if os.path.isdir(os.path.join(root, 'pip')):
        runs.append(_run([sys.executable, '-m', 'pip', 'cache', 'purge'],
                         {**environment, 'PIP_CACHE_DIR': os.path.join(root, 'pip')}))
    for run in runs:
        logger.info('[tool-cache] %s exit=%s %s', ' '.join(run['argv'][1:]), run['exit'], run['detail'])
    return runs


def schedule_prune(cache_root: str) -> bool:
    """Prune CACHE_ROOT on a background thread; coalesce requests while one runs.

    Returns False when a running prune absorbed the request (it runs once more).
    """
    root = os.path.abspath(cache_root)
    with _lock:
        if root in _running:
            _pending.add(root)
            return False
        _running.add(root)
    threading.Thread(target=_prune_until_settled, args=(root,), daemon=True,
                     name='tool-cache-prune').start()
    return True


def _prune_until_settled(root: str) -> None:
    while True:
        try:
            prune(root)
        except Exception as error:  # retention is best effort; work never depends on it
            logger.warning('[tool-cache] prune of %s failed: %s', root, error)
        with _lock:
            if root in _pending:
                _pending.discard(root)
                continue
            _running.discard(root)
            return
