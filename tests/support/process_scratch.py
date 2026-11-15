"""Temporary directories owned by the process that created them.

Responsibility: module-level test scratch (bundled runtime sections, Sidecar
roots) that outlives no process. ``atexit`` alone never ran for xdist workers
that leave through ``os._exit`` or are killed, so every session leaked its
directories: 110 ``tofu-runtime-sections-*`` trees (5.5 GiB) on 2026-11-15.

Entry point: :func:`process_scratch_directory`. The directory is named after
the owning PID; creating one first removes this user's earlier directories of
the same prefix whose owner process has exited (the lifecycle event), and an
``atexit`` hook removes the caller's own on a normal exit. A directory whose
PID is alive, possibly reused by an unrelated process, is kept until a later
creation finds that PID gone. Standard library only.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import re
import shutil
import tempfile


def _owner_exited(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def remove_exited_owners(prefix: str, parent: Path | None = None) -> list[Path]:
    """Remove this user's ``<prefix><pid>-*`` directories whose PID has exited."""
    base = Path(parent or tempfile.gettempdir())
    pattern = re.compile(re.escape(prefix) + r'(\d+)-[^/]+\Z')
    uid = os.getuid() if hasattr(os, 'getuid') else None
    removed: list[Path] = []
    try:
        entries = list(os.scandir(base))
    except OSError:
        return removed
    for entry in entries:
        match = pattern.match(entry.name)
        if match is None or int(match.group(1)) == os.getpid():
            continue
        try:
            status = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not entry.is_dir(follow_symlinks=False) or (uid is not None and status.st_uid != uid):
            continue
        if _owner_exited(int(match.group(1))):
            shutil.rmtree(entry.path, ignore_errors=True)
            removed.append(Path(entry.path))
    return removed


def process_scratch_directory(prefix: str, parent: Path | None = None) -> Path:
    """Create ``<prefix><pid>-<random>``, removed at exit or by a later creation."""
    remove_exited_owners(prefix, parent)
    directory = Path(tempfile.mkdtemp(prefix=f'{prefix}{os.getpid()}-',
                                      dir=None if parent is None else str(parent)))
    atexit.register(shutil.rmtree, directory, True)
    return directory
