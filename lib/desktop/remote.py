"""Remote-worktree binding contract (RWA 拍板 3A — same-name routing).

One shared home for the master switch + the cfg binding shape, consumed by
BOTH the projection layer (``ToolContext.project_remote`` →
``with_remote_hint``) and the execution-routing layer
(``handlers/project.py`` → bridge command). Keeping the contract here —
not re-parsed in two places — is what stops the tool schema and the
executor from disagreeing about whether a conversation is remote.
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)


def remote_worktree_enabled() -> bool:
    """Master switch (总闸). Default OFF = byte-identical legacy behaviour."""
    return (os.environ.get('TOFU_REMOTE_WORKTREE', '')
            or '').strip().lower() in ('1', 'true', 'yes', 'on')


def remote_worktree_binding(cfg):
    """``cfg['project_remote']`` → ``{'agent_id', 'root'}`` or None.

    The binding contract (set by the P4 entry points): a dict naming one
    registered agent + one of ITS declared share_roots. Both keys are
    required; a partial binding is treated as absent (fail-closed).
    """
    if not remote_worktree_enabled():
        return None
    remote = (cfg or {}).get('project_remote')
    if isinstance(remote, dict) and remote.get('agent_id') and remote.get('root'):
        return {'agent_id': str(remote['agent_id']),
                'root': str(remote['root'])}
    return None
