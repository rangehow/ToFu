"""Desktop Agent — local config persistence.

Tiny JSON store for agent-local settings that must survive restarts —
currently the stable ``agent_id`` (RWA P0 registration frame, see
docs/REMOTE_WORKTREE_DESIGN.md §3.2). Path: ``TOFU_DESKTOP_CONFIG`` env
var, else ``~/.tofu/desktop_agent.json``.
"""

import os

from lib.json_store import read_json, write_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

_DEFAULT_CONFIG = {
    'agent_id': '',  # generated on first start by _run._ensure_agent_id
}


def config_path():
    return (os.environ.get('TOFU_DESKTOP_CONFIG')
            or os.path.expanduser('~/.tofu/desktop_agent.json'))


def load_config():
    """Read the agent config, merged over defaults. Never raises."""
    cfg = dict(_DEFAULT_CONFIG)
    data = read_json(config_path(), default=None)
    if isinstance(data, dict):
        cfg.update(data)
    return cfg


def save_config(cfg):
    """Persist the agent config atomically (creates the parent dir)."""
    path = config_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    write_json_atomic(path, cfg)
    logger.debug('[Agent] config saved to %s', path)
