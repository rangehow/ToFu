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
    # RWA P1: [{name, path}] — the agent's declared project worktrees;
    # project_* commands are confined to these roots (constraint ⑤).
    'share_roots': [],
}


def config_path():
    return (os.environ.get('TOFU_DESKTOP_CONFIG')
            or os.path.expanduser('~/.tofu/desktop_agent.json'))


def merge_cli_roots(existing_roots, cli_specs):
    """Merge ``--root NAME=PATH`` specs into the persisted share_roots list.

    Pure logic (no I/O) so the CLI merge is unit-testable. The CLI wins on a
    name collision (path updated in place, list position kept); new names
    append in declaration order. ``~`` is expanded. Raises ValueError on a
    malformed spec (missing ``=`` / empty name / empty path).
    """
    roots = [dict(r) for r in (existing_roots or [])
             if isinstance(r, dict) and r.get('name')]
    by_name = {str(r['name']): r for r in roots}
    order = [str(r['name']) for r in roots]
    for spec in cli_specs or []:
        if not spec or '=' not in spec:
            raise ValueError(f'--root must be NAME=PATH, got {spec!r}')
        name, path = spec.split('=', 1)
        name = name.strip()
        path = os.path.expanduser(path.strip())
        if not name or not path:
            raise ValueError(f'--root must be NAME=PATH, got {spec!r}')
        if name in by_name:
            by_name[name]['path'] = path
        else:
            by_name[name] = {'name': name, 'path': path}
            order.append(name)
    return [by_name[n] for n in order]


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
