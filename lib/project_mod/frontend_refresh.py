"""Rebuild a Tofu checkout's frontend bundle in the background after a landing.

Responsibility: when a landing (``checkout_landing.publish``: ``git_land`` and
reviewed project deliveries) changes a Vite authoring input of a Tofu source
checkout, start that checkout's own lifecycle preparation
(``serverctl.py prepare-frontend --parts frontend``) detached, so the next
restart's preflight finds a valid bundle instead of rebuilding it on the
restart's critical path (audit 2026-11-18 #12: "Vite i18n chunks are stale"
cost 106.6 s inside the 02:09 restart, after a 01:48 landing changed locales).

The preparation owns every policy: its cross-process build lock, the fast
validity check, rebuilding while sources keep changing under a build. A
landing that changes nothing it reads starts nothing; a checkout that is not a
Tofu source checkout (any other project) starts nothing. Starting is advice:
a failure is logged and never fails the published landing.

Entry point: ``refresh_after_landing(checkout, paths)``.
Dependencies: ``frontend/authoring-inputs.json`` of the landed checkout (the
inventory ``lib.vite_assets`` validates against) and its ``serverctl.py``.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from lib.log import get_logger

logger = get_logger(__name__)

AUTHORING_INPUTS = 'frontend/authoring-inputs.json'
SOURCE_ROOT = 'frontend/src/'
REFRESH_LOG = 'frontend-refresh.log'
OPERATION = 'post-landing frontend refresh'


def changed_authoring_inputs(checkout: Path, paths) -> list[str]:
    """The landed ``paths`` that the checkout's published Vite graph is built from."""
    try:
        inventory = json.loads((checkout / AUTHORING_INPUTS).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    config = set(inventory.get('configPaths') or ())
    suffixes = tuple(inventory.get('sourceSuffixes') or ())
    return [path for path in paths if path in config or path == AUTHORING_INPUTS
            or (path.startswith(SOURCE_ROOT) and path.lower().endswith(suffixes))]


def refresh_after_landing(checkout, paths) -> subprocess.Popen | None:
    """Start the checkout's frontend preparation when the landing changed its inputs."""
    checkout = Path(checkout)
    if not (checkout / 'serverctl.py').is_file():
        return None
    changed = changed_authoring_inputs(checkout, paths)
    if not changed:
        return None
    from lib.tools.process_environment import inherited_child_environment
    environment = inherited_child_environment()
    # The checkout's own deployment decides whether its lifecycle builds the
    # frontend (its .env), not the role of the process that landed (an
    # execution host serves no frontend).
    environment.pop('TOFU_PROCESS_ROLE', None)
    from lib.runtime.runtime_paths import logs_root
    log_path = Path(logs_root()) / REFRESH_LOG
    try:
        with open(log_path, 'ab') as log:
            process = subprocess.Popen(
                [sys.executable, str(checkout / 'serverctl.py'), 'prepare-frontend',
                 '--parts', 'frontend', '--operation', OPERATION],
                cwd=checkout, env=environment, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as error:
        logger.warning('[FrontendRefresh] %s: preparation after the landing could not start '
                       '(%s); the next restart rebuilds it', checkout, error)
        return None
    logger.info('[FrontendRefresh] %s: landing changed %d frontend input(s) (%s); preparation '
                'pid=%d started, output in %s', checkout, len(changed), ', '.join(changed[:5]),
                process.pid, log_path)
    return process
