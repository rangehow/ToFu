"""lib/self_update/_git.py — git executable resolution and raw runners.

``_git_exe`` (cached PATH/Windows probe), ``_run_git`` (subprocess in the
project root), ``git_available`` (is this a checkout?) and ``_head_sha``.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

from lib.self_update._config import _ROOT

from lib.log import get_logger

logger = get_logger(__name__)

_git_exe_cache: Optional[str] = None


def _git_exe() -> str:
    """Resolve the git executable, falling back to common install dirs.

    On Windows the server is often launched from a context that lacks
    Git's bin/ on PATH (e.g. a double-clicked launcher), so a bare
    ``'git'`` would raise FileNotFoundError and make the UI wrongly
    report "not a git checkout". We therefore probe PATH via
    ``shutil.which`` first, then the standard Windows install locations.
    Result is cached for the process lifetime. Returns ``'git'`` as a
    last resort so the OS still gets a chance to resolve it.
    """
    global _git_exe_cache
    if _git_exe_cache:
        return _git_exe_cache

    import shutil
    found = shutil.which('git')
    if found:
        _git_exe_cache = found
        return found

    candidates = []
    if os.name == 'nt':
        for base in (os.environ.get('ProgramFiles', r'C:\Program Files'),
                     os.environ.get('ProgramFiles(x86)',
                                    r'C:\Program Files (x86)'),
                     os.environ.get('LocalAppData', '')):
            if base:
                candidates.append(os.path.join(base, 'Git', 'cmd', 'git.exe'))
                candidates.append(os.path.join(base, 'Git', 'bin', 'git.exe'))
    for c in candidates:
        if c and os.path.isfile(c):
            _git_exe_cache = c
            logger.info('[Update] Resolved git via fallback: %s', c)
            return c

    _git_exe_cache = 'git'
    return 'git'


def _run_git(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a git command in the project root, capturing output.

    Raises FileNotFoundError if git is not installed (caller handles it).
    """
    return subprocess.run(
        [_git_exe(), *args],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def git_available() -> bool:
    """True if this is a git checkout and git is on PATH."""
    if not os.path.isdir(os.path.join(_ROOT, '.git')):
        return False
    try:
        cp = _run_git(['rev-parse', '--git-dir'])
        return cp.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning('[Update] git not available: %s', e)
        return False



def _head_sha() -> Optional[str]:
    """Current HEAD commit SHA, or None if it can't be read."""
    try:
        cp = _run_git(['rev-parse', 'HEAD'])
        if cp.returncode == 0:
            return cp.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning('[Update] rev-parse HEAD failed: %s', e)
    return None

