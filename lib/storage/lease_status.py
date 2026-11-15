"""Read-only status of the project storage lease held by a storage authority.

Responsibility
--------------
Answer "is the project's storage lease held, and by whom" for the storage
supervisor (deferring a spawn) and the lifecycle manager (drain grace, orphan
reaping, the manager API). The OS lock on ``.storage-sidecar.lock`` is the sole
authority; the JSON stamp ``ProjectLease`` writes beside it is diagnostic
metadata only.

Entry point: ``read_storage_lease_status(data_dir)``.

Dependencies: the standard library and ``runtime_guards`` only, so packaged
compositions without the host lifecycle manager (``tofu-agent`` consumers
such as plugins) can start a supervised storage child.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import socket
import time
from typing import Any

from runtime_guards import pid_is_alive

_OFFLINE_STORAGE_COMMAND_LABELS = (
    ('scripts/storage_deep_clean.py', 'SQLite deep clean'),
    ('scripts/migrate_sqlite_to_postgres.py',
     'SQLite to PostgreSQL migration'),
)
_OFFLINE_STORAGECTL_COMMANDS = frozenset({
    'baseline', 'integrity-check', 'restore', 'handoff',
})


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding='utf-8') as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ''


def _proc_cmdline(pid: int) -> str | None:
    try:
        return Path(f'/proc/{int(pid)}/cmdline').read_bytes().replace(b'\0', b' ').decode(
            'utf-8', errors='replace').strip()
    except (OSError, ValueError, TypeError):
        return None


def _legacy_storage_lease_owner(cmdline: str | None) -> tuple[str, str]:
    """Classify old lease stamps without exposing the holder's command line."""
    command = str(cmdline or '')
    for marker, label in _OFFLINE_STORAGE_COMMAND_LABELS:
        if marker in command:
            return 'offline_maintenance', label
    if 'scripts/storagectl.py' in command:
        try:
            arguments = set(shlex.split(command))
        except ValueError:
            arguments = set(command.split())
        operation = next(
            (item for item in _OFFLINE_STORAGECTL_COMMANDS
             if item in arguments),
            None,
        )
        if operation:
            return 'offline_maintenance', f'Storage {operation}'
    if '-m lib.storage_authority_sql' in command:
        return 'storage_authority_sql', 'Storage sidecar'
    # The native Tofu-DB authority (``tofu-db serve``) is exec'd by
    # lib.storage_authority_sql.native_launch; it is the same child sidecar role,
    # never an independent daemon.
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if (len(tokens) >= 2
            and os.path.basename(tokens[0]).lower() in ('tofu-db', 'tofu-db.exe')
            and tokens[1] == 'serve'):
        return 'storage_authority_sql', 'Storage sidecar'
    return 'unknown', 'Storage operation'


def read_storage_lease_status(data_dir: str | Path) -> dict[str, Any]:
    """Inspect one storage lease using its OS lock as the sole authority.

    The JSON stamp is diagnostic metadata only.  A stale ``status=running``
    stamp never blocks startup; conversely, an unreadable held lock remains a
    fail-closed unknown owner.  Returned data deliberately excludes command
    lines, lease IDs and authority paths so the manager API cannot leak
    operator arguments or credentials.
    """
    root = Path(data_dir)
    lock_path = root / '.storage-sidecar.lock'
    lease_path = root / '.storage-sidecar-lease.json'
    result: dict[str, Any] = {
        'held': False,
        'kind': None,
        'label': None,
        'pid': None,
        'host': None,
        'startedAt': None,
        'ageSeconds': None,
        'holderVerified': False,
    }
    if not lock_path.is_file():
        return result
    try:
        handle = lock_path.open('r+b')
    except OSError:
        return {
            **result,
            'held': True,
            'kind': 'unknown',
            'label': 'Storage operation',
        }
    try:
        try:
            if os.name == 'nt':  # pragma: no cover - Windows CI
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return result
        except (OSError, BlockingIOError):
            result['held'] = True
    finally:
        handle.close()

    stamp = _read_json(lease_path)
    host = str(stamp.get('host') or '').strip()[:255] or None
    try:
        pid = int(stamp.get('pid'))
        if pid <= 1:
            pid = None
    except (TypeError, ValueError):
        pid = None
    try:
        started_at = float(stamp.get('started_unix_ms')) / 1000.0
        if started_at <= 0:
            started_at = None
    except (TypeError, ValueError):
        started_at = None

    kind = str(stamp.get('owner_kind') or '').strip().lower()
    label = ' '.join(str(stamp.get('owner_label') or '').split())[:120]
    # ProjectLease marks the native Tofu-DB authority 'tofudb'; normalize it
    # to the sidecar role it plays so drain grace and orphan reaping apply.
    known_kinds = {
        'offline_maintenance': 'offline_maintenance',
        'storage_authority_sql': 'storage_authority_sql',
        'storage_operation': 'storage_operation',
        'tofudb': 'storage_authority_sql',
    }
    kind = known_kinds.get(kind, '')
    same_host = bool(host and host == _hostname())
    process_alive = bool(pid and same_host and pid_is_alive(pid))
    if process_alive:
        inferred_kind, inferred_label = _legacy_storage_lease_owner(
            _proc_cmdline(pid))
        if not kind or inferred_kind == 'offline_maintenance':
            kind = inferred_kind
            label = inferred_label
    if not kind:
        kind = 'unknown'
    if not label:
        label = {
            'offline_maintenance': 'Offline storage maintenance',
            'storage_authority_sql': 'Storage sidecar',
            'storage_operation': 'Storage operation',
        }.get(kind, 'Storage operation')
    age_seconds = (
        max(0.0, time.time() - started_at) if started_at is not None else None)
    return {
        **result,
        'kind': kind,
        'label': label,
        'pid': pid,
        'host': host,
        'startedAt': started_at,
        'ageSeconds': round(age_seconds, 1) if age_seconds is not None else None,
        'holderVerified': process_alive,
    }
