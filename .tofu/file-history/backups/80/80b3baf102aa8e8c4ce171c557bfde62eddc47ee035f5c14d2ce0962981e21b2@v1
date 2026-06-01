"""lib/env_compat.py — Read TOFU_* env vars with CHATUI_* legacy fallback.

The project was originally branded ChatUI; the canonical name is now Tofu.
All new code reads ``TOFU_*`` env vars via :func:`getenv_compat`, but to
avoid breaking existing deployments and CI configurations the helper also
honours the legacy ``CHATUI_*`` name when ``TOFU_*`` is unset.

Usage
-----

.. code-block:: python

    from lib.env_compat import getenv_compat

    # Read TOFU_DB_PATH, fall back to CHATUI_DB_PATH, fall back to default
    db_path = getenv_compat('TOFU_DB_PATH', 'CHATUI_DB_PATH',
                            default='data/tofu.db')

A one-time deprecation warning is logged when the legacy name is observed
so operators know to update their environment. Use ``warn=False`` to
suppress (e.g. for vendored helper scripts).
"""

import os
import threading

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['getenv_compat', 'promote_legacy_env']

_warned_lock = threading.Lock()
_warned = set()


def getenv_compat(*names, default=''):
    """Return the first non-empty env var among ``names`` else ``default``.

    Conventionally pass the canonical ``TOFU_*`` name first, the legacy
    ``CHATUI_*`` name second. When the legacy name resolves the value, a
    one-time deprecation warning is logged.
    """
    if not names:
        return default
    for i, name in enumerate(names):
        value = os.environ.get(name)
        if value is None or value == '':
            continue
        if i > 0:
            with _warned_lock:
                if name not in _warned:
                    _warned.add(name)
                    logger.warning(
                        '[env] Legacy env var %s is deprecated; please rename to %s.',
                        name, names[0])
        return value
    return default


def promote_legacy_env():
    """Copy ``CHATUI_*`` env vars to matching ``TOFU_*`` if the latter is unset.

    Called once at process startup so subprocesses spawned by the server
    (e.g. PG bootstrap, Codex CLI, Claude Code subprocess) see the new
    canonical names without needing per-call ``getenv_compat`` plumbing.
    Idempotent — safe to call multiple times.
    """
    promoted = []
    for legacy_name, legacy_value in list(os.environ.items()):
        if not legacy_name.startswith('CHATUI_'):
            continue
        canonical_name = 'TOFU_' + legacy_name[len('CHATUI_'):]
        if canonical_name in os.environ and os.environ[canonical_name] != '':
            continue
        os.environ[canonical_name] = legacy_value
        promoted.append((legacy_name, canonical_name))
    if promoted:
        logger.info('[env] Promoted %d legacy CHATUI_* env var(s) to TOFU_* for this process: %s',
                    len(promoted), ', '.join(f'{a}->{b}' for a, b in promoted))
    return promoted
