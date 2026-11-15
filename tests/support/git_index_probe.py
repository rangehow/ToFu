"""A checkout whose index a lock-taking Git read visibly rewrites.

Porcelain ``git diff`` against the working tree and ``git describe --dirty``
refresh a stale index and rewrite it under ``index.lock`` even with
``--no-optional-locks`` (Git 2.34.1); plain ``git status`` takes the lock too.
One tracked file with stat-only drift (same bytes, older mtime) makes any such
refresh rewrite the index, so ``IndexProbe.unchanged()`` turns False after a
read that took the lock. ``writer_holds_lock`` reserves the index as a
concurrent writer would; a read must still answer and leave that lock alone.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

_COMMIT = ('-c', 'user.name=Index Probe', '-c', 'user.email=index-probe@example.invalid',
           '-c', 'gc.auto=0', 'commit', '-qm', 'baseline')


def _index_state(index: Path) -> tuple[bytes, int, int]:
    info = index.stat()
    return index.read_bytes(), info.st_mtime_ns, info.st_ino


@dataclass(frozen=True)
class IndexProbe:
    root: Path
    index: Path
    lock: Path
    before: tuple[bytes, int, int]

    def unchanged(self) -> bool:
        return _index_state(self.index) == self.before


def stat_dirty_checkout(root: Path, *, name: str = 'tracked.py', text: str = 'VALUE = 1\n',
                        writer_holds_lock: bool = False) -> IndexProbe:
    """Commit NAME at ROOT, then age its mtime so only its stat data is stale."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    tracked = root / name
    tracked.write_text(text)
    subprocess.run(['git', '-C', str(root), 'add', name], check=True)
    subprocess.run(['git', '-C', str(root), *_COMMIT], check=True)
    info = tracked.stat()
    os.utime(tracked, ns=(info.st_atime_ns, info.st_mtime_ns - 2_000_000_000))
    admin = Path(subprocess.run(['git', '-C', str(root), 'rev-parse', '--absolute-git-dir'],
                                capture_output=True, text=True, check=True).stdout.strip())
    index, lock = admin / 'index', admin / 'index.lock'
    if writer_holds_lock:
        lock.write_bytes(b'')
    return IndexProbe(root, index, lock, _index_state(index))
