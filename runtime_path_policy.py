"""Canonical data/log base selection without application or storage imports.

Entry points: resolve_runtime_base for explicit diagnostic environments;
get_runtime_base for the one process startup decision shared by data and logs;
instance_heartbeat_path for the host-local loop heartbeat of one instance.
Only frozen writable startup probes create an exclusive temporary file. Source
and read-only resolution never create directories. Dependencies: stdlib only.
"""
from __future__ import annotations

from collections.abc import Mapping
from threading import Lock
import hashlib
import logging
import os
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_logger = logging.getLogger(__name__)


def _per_user_base(environment: Mapping[str, str]) -> str:
    if sys.platform.startswith('win'):
        base = environment.get('LOCALAPPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'Tofu')
    if sys.platform == 'darwin':
        return os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'Tofu')
    base = environment.get('XDG_DATA_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'share')
    return os.path.join(base, 'Tofu')


def _populated(path: str) -> bool:
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is not None
    except OSError:
        return False


def _probe_writable(path: str) -> bool:
    try:
        # The executable directory already exists. Never truncate a fixed
        # filename or follow a pre-existing probe symlink into user content.
        with tempfile.TemporaryFile(prefix='.tofu-write-probe-', dir=path):
            pass
        return True
    except OSError:
        return False


def _silent(*_arguments: object) -> None:
    return None


def resolve_runtime_base(
    environment: Mapping[str, str] | None = None,
    *,
    repository_root: str | os.PathLike[str] = _REPO_ROOT,
    allow_probe: bool = True,
    report: bool = True,
) -> str:
    """Return the data/log base for *environment*; ``report`` logs the choice.

    A process logs its own startup decision; a lifecycle reader that locates
    another process's data root on every status poll passes ``report=False``.
    """
    environment = os.environ if environment is None else environment
    note = _logger.info if report else _silent
    explicit = environment.get('TOFU_DATA_DIR', '')
    if explicit:
        explicit = os.path.abspath(explicit)
        base = os.path.dirname(explicit) if os.path.basename(explicit) == 'data' else explicit
        note('Data layout: explicit TOFU_DATA_DIR override → %s', base)
        return base
    if getattr(sys, 'frozen', False):
        if not allow_probe:
            raise ValueError('read-only frozen diagnostics require an explicit TOFU_DATA_DIR')
        sibling = os.path.dirname(sys.executable)
        return sibling if _probe_writable(sibling) else os.path.abspath(_per_user_base(environment))
    repository_root = os.path.abspath(repository_root)
    layout = (environment.get('TOFU_DATA_LAYOUT') or 'auto').strip().lower()
    if layout == 'intree':
        note('Data layout: in-tree repo root (TOFU_DATA_LAYOUT=intree) → %s', repository_root)
        return repository_root
    if layout not in ('auto', 'xdg'):
        _logger.warning('Unknown TOFU_DATA_LAYOUT=%r; treating as "auto"', layout)
    if layout != 'xdg' and _populated(os.path.join(repository_root, 'data')):
        note('Data layout: existing in-tree data/ found → %s', repository_root)
        return repository_root
    base = os.path.abspath(_per_user_base(environment))
    note('Data layout: %s → %s',
         'per-user XDG (TOFU_DATA_LAYOUT=xdg)' if layout == 'xdg'
         else 'fresh source checkout (no populated in-tree data/)', base)
    return base


# Host-local, never under data/ (the FUSE mount whose stalls the heartbeat
# detects). One file per instance: the default directory is shared by every
# Tofu on the host, so a scratch instance must not stamp the live one's file.
_DEFAULT_HEARTBEAT_ROOT = '/tmp/tofu/heartbeat'
HEARTBEAT_FILE_NAME = 'server.heartbeat'


def instance_heartbeat_path(data_directory: str | os.PathLike[str],
                            environment: Mapping[str, str] | None = None) -> str:
    """Heartbeat sidecar of the server whose instance lock lives in *data_directory*.

    ``TOFU_HEARTBEAT_DIR`` names the directory explicitly. Otherwise the file
    sits under the host-local root in a directory named by the digest of the
    data directory's real path, so each instance lock has its own heartbeat.
    """
    environment = os.environ if environment is None else environment
    explicit = (environment.get('TOFU_HEARTBEAT_DIR') or '').strip()
    if explicit:
        return os.path.join(explicit, HEARTBEAT_FILE_NAME)
    identity = hashlib.sha256(os.path.realpath(os.fspath(data_directory)).encode('utf-8')).hexdigest()
    return os.path.join(_DEFAULT_HEARTBEAT_ROOT, identity, HEARTBEAT_FILE_NAME)


_startup_base: str | None = None
_startup_lock = Lock()


def get_runtime_base() -> str:
    """One startup choice; data creation cannot change the later log decision."""
    global _startup_base
    with _startup_lock:
        if _startup_base is None:
            _startup_base = resolve_runtime_base()
        return _startup_base
