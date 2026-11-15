"""Process capability shared by the desktop launcher and its server child.

Responsibility: own the one in-memory credential the packaged desktop app's
in-process agent presents to ``/api/desktop/poll``. The launcher (tray
process) runs the agent; the server is its child process, so the value is
minted by the launcher and handed to the child through one environment
variable, which this module removes from ``os.environ`` on import so tool
subprocesses spawned by the server never inherit it.

Entry points: :func:`process_agent_token`, :func:`child_environment`.
Dependencies: standard library only (imported early in ``server.py``).
"""

from __future__ import annotations

import os
import secrets

_ENVIRONMENT_NAME = 'TOFU_DESKTOP_AGENT_TOKEN'

_TOKEN = os.environ.pop(_ENVIRONMENT_NAME, '').strip() or secrets.token_urlsafe(32)


def process_agent_token() -> str:
    return _TOKEN


def child_environment() -> dict[str, str]:
    """Environment entry that makes a child server accept this capability."""
    return {_ENVIRONMENT_NAME: _TOKEN}
