"""lib/self_update/_requirements.py — dependency (requirements.txt) handling.

``_requirements_changed`` (SHA-range diff) and ``_install_requirements``
(pip install against the running interpreter).
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from lib.log import log_context
from lib.self_update._config import _PIP_TIMEOUT, _REQUIREMENTS, _ROOT
from lib.self_update._git import _run_git

from lib.log import get_logger

logger = get_logger(__name__)

def _requirements_changed(before_sha: Optional[str],
                          after_sha: Optional[str]) -> bool:
    """True if ``requirements.txt`` differs between two commits.

    Compares by SHA range rather than scraping ``git pull`` output, which
    is locale-dependent and unreliable. If either SHA is missing or the
    diff can't be computed, returns True (install defensively rather than
    silently skip a needed dependency).
    """
    if before_sha and after_sha and before_sha == after_sha:
        return False  # nothing was pulled
    if not before_sha or not after_sha:
        logger.warning('[Update] missing commit SHA — will install '
                       'dependencies defensively')
        return True
    try:
        cp = _run_git(['diff', '--name-only', before_sha, after_sha])
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning('[Update] requirements diff failed (%s) — will '
                       'install defensively', e)
        return True
    if cp.returncode != 0:
        logger.warning('[Update] requirements diff returned %d — will '
                       'install defensively', cp.returncode)
        return True
    changed = [p.strip() for p in cp.stdout.splitlines() if p.strip()]
    return _REQUIREMENTS in changed


def _install_requirements() -> dict:
    """Run ``pip install -r requirements.txt`` with the current interpreter.

    Returns ``{'ok': bool, 'detail': str}``. Uses ``sys.executable -m pip``
    so the install targets the SAME interpreter the server runs under
    (critical inside a conda env). Never raises — failures are captured.
    """
    req_path = os.path.join(_ROOT, _REQUIREMENTS)
    if not os.path.isfile(req_path):
        logger.info('[Update] No %s — skipping dependency install',
                    _REQUIREMENTS)
        return {'ok': True, 'detail': 'no requirements.txt'}

    cmd = [sys.executable, '-m', 'pip', 'install', '-r', req_path]
    logger.info('[Update] Installing dependencies: %s', ' '.join(cmd))
    with log_context('self_update.pip_install', logger=logger):
        try:
            cp = subprocess.run(cmd, cwd=_ROOT, capture_output=True,
                                text=True, timeout=_PIP_TIMEOUT)
        except (FileNotFoundError, subprocess.TimeoutExpired,
                subprocess.SubprocessError) as e:
            logger.error('[Update] pip install errored: %s', e, exc_info=True)
            return {'ok': False, 'detail': str(e)[:500]}

    if cp.returncode != 0:
        tail = (cp.stderr or cp.stdout or '')[-500:]
        logger.error('[Update] pip install failed (exit %d): %s',
                     cp.returncode, tail)
        return {'ok': False, 'detail': tail}

    logger.info('[Update] Dependencies installed successfully')
    return {'ok': True, 'detail': (cp.stdout or '')[-300:]}

